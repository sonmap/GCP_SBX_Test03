import copy
import importlib.util
import io
import json
import os
import shutil
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from request_fixtures import ENV, make_request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('provisioner', ROOT / '03.sandbox_deploy/03.1.request/main.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
TF = ROOT / '03.sandbox_deploy/03.2.terraform/deployments'


def schema_errors(payload):
    return [e.message for e in p.VALIDATOR.iter_errors(payload)]


@mock.patch.dict(os.environ, ENV)
class RequestRuleTests(unittest.TestCase):
    def test_new_and_migrated_samples_are_valid(self):
        for request in (make_request(), make_request(migrated=True), make_request(migrated=True, containers=3)):
            self.assertEqual(schema_errors(request), [])
            p.validate_semantics(request)

    def test_three_areas_share_one_host(self):
        for task, area in [('task01', 'general'), ('task11', 'sa'), ('task21', 'com')]:
            request = make_request()
            request['task']['name'] = task
            request['identity']['group_email'] = f'pgrp-gcp-dev-{task}@sonmap.net'
            project = f'gcp-sbx-edp-{task}'
            request['project']['project_id'] = project
            request['identity']['jupyter_gsa']['email'] = f'sa-sbx-{task}-notebook-1-user@{project}.iam.gserviceaccount.com'
            request['workload'].update(area=area, namespace=f'sbx-{area}-{task}')
            request['workload']['containers'][0]['base_path'] = f'/{area}/{task}/'
            self.assertEqual(schema_errors(request), [])
            p.validate_semantics(request)
        request = make_request()
        request['workload'].update(area='common', namespace='sbx-common-sbx03')
        with self.assertRaisesRegex(ValueError, 'workload.area'):
            p.validate_semantics(request)

    def test_stages_follow_what_the_request_manages(self):
        self.assertEqual(p.enabled_stages(make_request()),
                         ['05-group', '10-project', '30-project-iam', '40-data', '50-workload'])
        # A migrated task already has its group, project and data.
        self.assertEqual(p.enabled_stages(make_request(migrated=True)), ['50-workload'])

    def test_image_must_be_digest_pinned(self):
        request = make_request(migrated=True)
        request['workload']['containers'][0]['image'] = request['workload']['containers'][0]['image'].split('@')[0] + ':latest'
        self.assertTrue(schema_errors(request))

    def test_environment_policy(self):
        cases = {
            'workload.area': lambda r: r['workload'].update(area='mvno', namespace='sbx-mvno-rsch'),
            'image registry': lambda r: r['workload']['containers'][0].update(image='asia-northeast3-docker.pkg.dev/gcp-sbx-gke01/x/y@sha256:' + 'ab' * 32),
            'workload.namespace': lambda r: r['workload'].update(namespace='other'),
            'image registry': lambda r: r['workload']['containers'][0].update(image='docker.io/x/y@' + 'sha256:' + 'ab' * 32),
            'approved Jupyter domain': lambda r: r['workload']['containers'][0].update(host='sbx.example.com'),
            'memory_gib per cpu': lambda r: r['workload'].update(cpu=4, memory_gib=64),
            'zone of gke.location': lambda r: r['workload']['storage'].update(zone='us-central1-a'),
            'existing_disk': lambda r: r['workload']['storage'].pop('existing_disk'),
            'task project': lambda r: r['identity']['jupyter_gsa'].update(email='sa-other-user@gcp-sbx-edp-other.iam.gserviceaccount.com'),
            'group_email must be': lambda r: r['identity'].update(group_email='pgrp-gcp-dev-other@sonmap.net'),
        }
        for message, mutate in cases.items():
            request = make_request(migrated=True)
            mutate(request)
            with self.assertRaisesRegex(ValueError, message):
                p.validate_semantics(request)

    def test_containers_in_one_pod_need_distinct_ports_and_routes(self):
        request = make_request(migrated=True, containers=2)
        request['workload']['containers'][1]['hub_port'] = 8081
        with self.assertRaisesRegex(ValueError, 'hub_port must be unique'):
            p.validate_semantics(request)
        request = make_request(migrated=True, containers=2)
        request['workload']['containers'][1]['base_path'] = request['workload']['containers'][0]['base_path']
        with self.assertRaisesRegex(ValueError, 'host \\+ base_path'):
            p.validate_semantics(request)

    def test_update_is_checked_against_applied_state(self):
        applied = make_request(migrated=True)
        with self.assertRaisesRegex(ValueError, 'action=CREATE first'):
            p.validate_against_applied(dict(applied, action='UPDATE'), None)

        # stop, resize up and image swap are ordinary updates
        update = copy.deepcopy(applied)
        update['action'] = 'UPDATE'
        update['workload'].update(running=False, cpu=16, memory_gib=128)
        update['workload']['storage']['size_gb'] = 200
        p.validate_against_applied(update, applied)

        for path, value in [(('workload', 'namespace'), 'sbx-general-other'),
                            (('workload', 'storage', 'existing_disk'), 'disk-other'),
                            (('workload', 'storage', 'zone'), 'asia-northeast3-b'),
                            (('project', 'project_id'), 'gcp-sbx-edp-other')]:
            changed = copy.deepcopy(update)
            target = changed
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.assertRaisesRegex(ValueError, 'cannot change'):
                p.validate_against_applied(changed, applied)

        shrink = copy.deepcopy(update)
        shrink['workload']['storage']['size_gb'] = 50
        with self.assertRaisesRegex(ValueError, 'cannot shrink'):
            p.validate_against_applied(shrink, applied)


@mock.patch.dict(os.environ, ENV)
class BundleAndBuildTests(unittest.TestCase):
    def assemble(self, request):
        written = {}
        bucket = mock.Mock()
        bucket.name = ENV['BUNDLE_BUCKET']
        bucket.blob.side_effect = lambda name: name

        def download(source, destination):
            shutil.copytree(TF, destination / 'deployments')
            return destination
        with mock.patch.object(p.storage, 'Client') as client, \
                mock.patch.object(p, 'download_source_archive', side_effect=download) as fetched, \
                mock.patch.object(p, 'upload_immutable', side_effect=lambda name, data, ctype: written.setdefault(name, data)):
            client.return_value.bucket.return_value = bucket
            prefix, manifest = p.assemble_bundles(request, b'{}')
        return written, manifest, fetched

    def test_new_task_bundles_five_stages(self):
        written, manifest, _ = self.assemble(make_request())
        self.assertEqual(set(manifest['bundles']), {'05-group', '10-project', '30-project-iam', '40-data', '50-workload'})
        workload_zip = next(v for k, v in written.items() if k.endswith('/workload.zip'))
        with zipfile.ZipFile(io.BytesIO(workload_zip)) as archive:
            names = archive.namelist()
            self.assertIn('templates/jupyterhub_config.py.tftpl', names)
            variables = json.loads(archive.read('terraform.auto.tfvars.json'))
            self.assertEqual(variables['namespace'], 'sbx-general-sbx03')
        data_zip = next(v for k, v in written.items() if k.endswith('/data.zip'))
        with zipfile.ZipFile(io.BytesIO(data_zip)) as archive:
            variables = json.loads(archive.read('terraform.auto.tfvars.json'))
            self.assertTrue(variables['create_jupyter_gsa'])
            self.assertEqual(variables['jupyter_gsa_email'],
                             'sa-sbx-sbx03-notebook-1-user@gcp-sbx-edp-sbx03.iam.gserviceaccount.com')
            # The Workload Identity binding moved to 50-workload, next to the KSA.
            self.assertNotIn('workloadIdentityUser', archive.read('main.tf').decode())

    def test_migrated_task_bundles_only_the_workload(self):
        written, manifest, _ = self.assemble(make_request(migrated=True))
        self.assertEqual(set(manifest['bundles']), {'50-workload'})
        self.assertTrue(any(k.endswith('/approved-request.json') for k in written))

    def build_spec(self, request):
        with mock.patch.object(p.cloudbuild_v1, 'CloudBuildClient'), \
                mock.patch.object(p.cloudbuild_v1, 'Build', side_effect=lambda spec: spec), \
                mock.patch.object(p.cloudbuild_v1, 'CreateBuildRequest', side_effect=lambda **kw: kw) as create:
            p.start_build(request, 'gs://r/approved/x/request.json', 'gs://b/requests/x/git-test123')
        return create.call_args.kwargs['build']

    def test_every_stage_is_an_infrastructure_manager_deployment(self):
        spec = self.build_spec(make_request())
        self.assertEqual([step['id'] for step in spec['steps']], ['terraform', 'alb'])
        script = spec['steps'][0]['args'][-1]
        self.assertEqual(script.count('gcloud infra-manager deployments apply'), 5)
        for suffix in ['group', 'project', 'project-iam', 'data', 'workload']:
            self.assertIn(f'/deployments/sbxtest01-sbx03-', script)
            self.assertIn(f'-{suffix}\" ', script)
        self.assertIn('--worker-pool=', script)
        self.assertIn('--tf-version-constraint=', script)
        # Order: workload last.
        self.assertGreater(script.index('-workload'), script.index('-data'))

    def test_migrated_task_has_one_deployment(self):
        script = self.build_spec(make_request(migrated=True))['steps'][0]['args'][-1]
        self.assertEqual(script.count('gcloud infra-manager deployments apply'), 1)
        self.assertIn('-workload', script)

    def test_alb_step_reads_the_request_from_gcs(self):
        alb = self.build_spec(make_request())['steps'][1]
        self.assertEqual(alb['args'], ['/opt/sandbox/reconcile_postdeploy.py',
                                       'gs://b/requests/x/git-test123/approved-request.json'])
        self.assertTrue(alb['name'].endswith('sbxtest01-automation-runner:git-test123'))


if __name__ == '__main__':
    unittest.main()
