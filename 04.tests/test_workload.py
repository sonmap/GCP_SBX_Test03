import importlib.util
import os
import re
import unittest
from pathlib import Path
from unittest import mock

from request_fixtures import ENV, make_request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('provisioner_w', ROOT / '03.sandbox_deploy/03.1.request/main.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
MODULE = ROOT / '03.sandbox_deploy/03.2.terraform/deployments/50-workload'


def declared_variables():
    text = (MODULE / 'variables.tf').read_text()
    names, required = set(), set()
    for match in re.finditer(r'^variable "([a-z_]+)" \{( type = \w+ \}$|\n.*?^\})', text, re.M | re.S):
        names.add(match.group(1))
        if 'default' not in match.group(2):
            required.add(match.group(1))
    return names, required


@mock.patch.dict(os.environ, ENV)
class WorkloadVariableTests(unittest.TestCase):
    def test_variables_match_the_module(self):
        names, required = declared_variables()
        for request in (make_request(), make_request(migrated=True, containers=3)):
            variables = p.workload_variables(request)
            self.assertLessEqual(set(variables), names)
            self.assertLessEqual(required, set(variables))

    def test_migrated_task_attaches_existing_disk_as_static_volume(self):
        variables = p.workload_variables(make_request(migrated=True))
        self.assertEqual(variables['existing_disk'], 'disk-sbx-rsch-data')
        self.assertEqual(variables['storage_class'], 'sbx-static-pd')
        self.assertEqual(variables['jupyter_gsa_email'], 'sa-sbx-rsch-notebook-1-user@gcp-sbx-edp-rsch.iam.gserviceaccount.com')
        self.assertEqual(variables['cluster_name'], 'gke-sbx-main-an3')

    def test_new_task_gets_dynamic_disk(self):
        variables = p.workload_variables(make_request())
        self.assertEqual(variables['existing_disk'], '')
        self.assertEqual(variables['storage_class'], 'standard-rwo')

    def test_stop_is_only_a_flag(self):
        request = make_request(migrated=True)
        request['workload']['running'] = False
        running = p.workload_variables(make_request(migrated=True))
        stopped = p.workload_variables(request)
        self.assertEqual({k for k in running if running[k] != stopped[k]}, {'running'})

    def test_login_mode_comes_from_the_environment(self):
        self.assertEqual(p.workload_variables(make_request())['hub_auth_mode'], 'oauth')
        with mock.patch.dict(os.environ, {'HUB_AUTH_MODE': 'dummy'}):
            self.assertEqual(p.workload_variables(make_request())['hub_auth_mode'], 'dummy')


class InfrastructureManagerFitTests(unittest.TestCase):
    """Infrastructure Manager owns state and runs Terraform 1.5.7."""

    def test_no_backend_and_terraform_157(self):
        for stage in ['05-group', '10-project', '30-project-iam', '40-data', '50-workload']:
            versions = (MODULE.parent / stage / 'versions.tf').read_text()
            self.assertNotRegex(versions, r'^\s*backend\s+"', stage)
            self.assertIn('>= 1.5.7', versions, stage)

    def test_cluster_access_without_kubeconfig(self):
        versions = (MODULE / 'versions.tf').read_text()
        self.assertNotIn('config_path', versions)
        self.assertIn('data.google_client_config.current.access_token', versions)
        self.assertIn('dns_endpoint_config', versions)

    def test_destroy_guard_lives_in_the_code(self):
        text = (MODULE / 'main.tf').read_text()
        for resource in ['kubernetes_namespace" "task', 'kubernetes_service_account" "jupyter',
                         'google_service_account_iam_member" "workload_identity',
                         'kubernetes_persistent_volume" "data', 'kubernetes_persistent_volume_claim" "data',
                         'kubernetes_service" "jupyter']:
            start = text.index(f'resource "{resource}"')
            end = text.find('\nresource "', start + 1)
            self.assertIn('prevent_destroy = true', text[start:end if end > 0 else None], resource)

    def test_single_writer_disk_rules(self):
        text = (MODULE / 'main.tf').read_text()
        self.assertIn('type = "Recreate"', text)
        self.assertIn('persistent_volume_reclaim_policy = "Retain"', text)

    def test_oauth_secret_only_in_oauth_mode(self):
        text = (MODULE / 'main.tf').read_text()
        self.assertIn('count = local.mount_oauth ? 1 : 0', text)
        template = (MODULE / 'templates/jupyterhub_config.py.tftpl').read_text()
        self.assertNotRegex(template, r'client_secret\s*=\s*["\']')


class LoginSwitchTests(unittest.TestCase):
    template = (MODULE / 'templates/jupyterhub_config.py.tftpl').read_text()

    def render(self, mode):
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

    def test_oauth_mode_reads_the_mounted_secret(self):
        self.assertIn('_secret("client_secret")', self.render('oauth'))


if __name__ == '__main__':
    unittest.main()
