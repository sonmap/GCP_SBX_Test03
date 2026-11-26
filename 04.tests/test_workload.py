import importlib.util
import re
import unittest
from pathlib import Path

from request_fixtures import make_request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('run_workload', ROOT / '03.sandbox_deploy/03.3.gke/run_workload.py')
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


class DestroyGuardTests(unittest.TestCase):
    def test_ordinary_operations_pass(self):
        plan = {'resource_changes': [
            change('kubernetes_deployment', 'update'),
            change('kubernetes_persistent_volume_claim', 'update'),
            change('kubernetes_config_map', 'delete', 'create'),
            change('kubernetes_namespace', 'no-op'),
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
