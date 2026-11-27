import importlib.util
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from request_fixtures import make_request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('run_terraform', ROOT / '03.sandbox_deploy/03.3.gke/run_terraform.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
MODULE = ROOT / '03.sandbox_deploy/03.2.terraform/deployments/50-workload'
ENV = {'KUBECONFIG': '/workspace/kubeconfig'}


def change(resource_type, *actions, name='x'):
    return {'address': f'{resource_type}.{name}', 'type': resource_type, 'change': {'actions': list(actions)}}


class WorkloadVariableTests(unittest.TestCase):
    def test_every_tfvar_is_a_declared_variable(self):
        declared = set(re.findall(r'^variable "([a-z_]+)"', (MODULE / 'variables.tf').read_text(), re.M))
        for request in (make_request(), make_request(migrated=True, containers=3)):
            self.assertLessEqual(set(w.workload_tfvars(request, ENV)), declared)
        required = {name for name, body in re.findall(r'^variable "([a-z_]+)" \{(.*?)^\}', (MODULE / 'variables.tf').read_text(), re.M | re.S)
                    if 'default' not in body} | set(re.findall(r'^variable "([a-z_]+)" \{ type = \w+ \}$', (MODULE / 'variables.tf').read_text(), re.M))
        self.assertLessEqual(required, set(w.workload_tfvars(make_request(), ENV)))

    def test_migrated_task_attaches_existing_disk_as_static_volume(self):
        variables = w.workload_tfvars(make_request(migrated=True), ENV)
        self.assertEqual(variables['existing_disk'], 'disk-sbx-rsch-data')
        self.assertEqual(variables['storage_class'], 'sbx-static-pd')
        self.assertEqual(variables['jupyter_gsa_email'], 'sa-sbx-rsch-notebook-1-user@gcp-sbx-edp-rsch.iam.gserviceaccount.com')
        self.assertEqual(variables['task_project_id'], 'gcp-sbx-edp-rsch')

    def test_new_task_gets_dynamic_disk(self):
        variables = w.workload_tfvars(make_request(), ENV)
        self.assertEqual(variables['existing_disk'], '')
        self.assertEqual(variables['storage_class'], 'standard-rwo')

    def test_stop_is_only_a_flag(self):
        request = make_request(migrated=True)
        request['workload']['running'] = False
        running = w.workload_tfvars(make_request(migrated=True), ENV)
        stopped = w.workload_tfvars(request, ENV)
        self.assertEqual({k for k in running if running[k] != stopped[k]}, {'running'})


class LoginSwitchTests(unittest.TestCase):
    template = (MODULE / 'templates/jupyterhub_config.py.tftpl').read_text()

    def render(self, mode):
        """Minimal stand-in for templatefile(): if/else/endif on auth_mode."""
        def pick(match):
            return match.group(1) if mode == 'oauth' else (match.group(2) or '')
        text = re.sub(r'%\{ if auth_mode == "oauth" ~\}\n(.*?)(?:%\{ else ~\}\n(.*?))?%\{ endif ~\}\n', pick, self.template, flags=re.S)
        self.assertNotIn('%{', text)
        text = re.sub(r'\$\{(\w+)\}', lambda m: '[]' if m.group(1) == 'admin_users' else '8081' if 'port' in m.group(1) else 'x', text)
        compile(text, 'jupyterhub_config.py', 'exec')
        return text

    def test_dummy_mode_has_no_login_server_and_no_secret(self):
        text = self.render('dummy')
        for absent in ['oauthenticator', 'client_secret', '/etc/jupyterhub/oauth', 'oauth_callback']:
            self.assertNotIn(absent, text)
        self.assertIn('authenticator_class = "dummy"', text)

    def test_oauth_mode_reads_the_mounted_secret(self):
        text = self.render('oauth')
        self.assertIn('_secret("client_secret")', text)
        self.assertNotIn('authenticator_class = "dummy"', text)

    def test_mode_comes_from_the_environment_not_the_request(self):
        request = make_request(migrated=True)
        self.assertEqual(w.workload_tfvars(request, ENV)['hub_auth_mode'], 'oauth')
        self.assertEqual(w.workload_tfvars(request, dict(ENV, HUB_AUTH_MODE='dummy'))['hub_auth_mode'], 'dummy')
        self.assertIn('mount_oauth = var.hub_config_managed && var.hub_auth_mode == "oauth"', (MODULE / 'main.tf').read_text())


class StageRunTests(unittest.TestCase):
    def test_stages_share_one_state_layout_and_run_in_order(self):
        calls = []
        with mock.patch.object(w, 'run', side_effect=lambda args, **kw: calls.append(args) or '{"resource_changes": []}'):
            for stage in ['10-project', '50-workload']:
                w.terraform_stage(stage, Path('/tmp/x'), 'task01', {'TF_STATE_BUCKET': 'bkt'})
        inits = [c for c in calls if c[:2] == ['terraform', 'init']]
        self.assertIn('-backend-config=prefix=tasks/task01/10-project', inits[0])
        self.assertIn('-backend-config=prefix=tasks/task01/50-workload', inits[1])
        self.assertTrue(all('-backend-config=bucket=bkt' in c for c in inits))
        self.assertEqual(w.STAGE_ORDER, ['05-group', '10-project', '30-project-iam', '40-data', '50-workload'])

    def test_guard_stops_before_apply(self):
        calls = []
        plan = json.dumps({'resource_changes': [change('google_storage_bucket', 'delete')]})
        with mock.patch.object(w, 'run', side_effect=lambda args, **kw: calls.append(args) or plan):
            with self.assertRaises(SystemExit):
                w.terraform_stage('40-data', Path('/tmp/x'), 'task01', {'TF_STATE_BUCKET': 'bkt'})
        self.assertFalse(any(c[:2] == ['terraform', 'apply'] for c in calls))


class DestroyGuardTests(unittest.TestCase):
    def test_ordinary_operations_pass(self):
        plan = {'resource_changes': [
            change('kubernetes_deployment', 'update'),
            change('kubernetes_persistent_volume_claim', 'update'),
            change('kubernetes_config_map', 'delete', 'create'),
            change('kubernetes_namespace', 'no-op'),
            change('google_cloud_identity_group_membership', 'delete'),
            change('google_project_iam_member', 'delete'),
        ]}
        self.assertEqual(w.destructive_changes(plan), [])

    def test_delete_or_replace_of_data_and_identity_is_blocked(self):
        plan = {'resource_changes': [
            change('kubernetes_persistent_volume_claim', 'delete', 'create'),
            change('kubernetes_persistent_volume', 'delete'),
            change('kubernetes_namespace', 'delete'),
            change('kubernetes_deployment', 'update'),
        ]}
        self.assertEqual(len(w.destructive_changes(plan)), 3)


class ModuleShapeTests(unittest.TestCase):
    text = (MODULE / 'main.tf').read_text()

    def test_single_writer_disk_rules(self):
        self.assertIn('type = "Recreate"', self.text)
        self.assertIn('persistent_volume_reclaim_policy = "Retain"', self.text)
        self.assertGreaterEqual(self.text.count('prevent_destroy = true'), 2)

    def test_state_is_per_task_and_holds_no_secret(self):
        versions = (MODULE / 'versions.tf').read_text()
        self.assertIn('backend "gcs" {}', versions)
        self.assertNotIn('kubernetes_secret', self.text)
        template = (MODULE / 'templates/jupyterhub_config.py.tftpl').read_text()
        self.assertNotRegex(template, r'client_secret\s*=\s*["\']')


if __name__ == '__main__':
    unittest.main()
