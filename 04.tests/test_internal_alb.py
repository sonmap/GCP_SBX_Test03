import copy
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

from request_fixtures import make_request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("reconcile", ROOT / "03.sandbox_deploy/03.3.gke/reconcile_postdeploy.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
PROJECT = "gcp-sbx-edp-gke01"
REGION = "asia-northeast3"
ENV = {"GCP_REGION": REGION}
DEFAULT = f"projects/{PROJECT}/regions/{REGION}/backendServices/bes-default"


def uri(name):
    return f"projects/{PROJECT}/regions/{REGION}/backendServices/{name}"


class UrlMapRouteTests(unittest.TestCase):
    def test_path_scheme_shares_one_host_between_tasks(self):
        config = {"defaultService": DEFAULT}
        self.assertTrue(module.merge_route(config, "sbx.sonmap.net", "/general/rsch/", uri("bes-rsch")))
        self.assertTrue(module.merge_route(config, "sbx.sonmap.net", "/general/rsce/", uri("bes-rsce")))
        self.assertEqual(len(config["hostRules"]), 1)
        matcher = config["pathMatchers"][0]
        self.assertEqual(matcher["defaultService"], DEFAULT)
        self.assertEqual(matcher["pathRules"][0]["paths"], ["/general/rsch", "/general/rsch/*"])
        self.assertEqual(len(matcher["pathRules"]), 2)

    def test_rerun_changes_nothing(self):
        config = {"defaultService": DEFAULT}
        module.merge_route(config, "sbx.sonmap.net", "/general/rsch/", uri("bes-rsch"))
        before = copy.deepcopy(config)
        full = "https://www.googleapis.com/compute/v1/" + uri("bes-rsch")
        config["pathMatchers"][0]["pathRules"][0]["service"] = full
        before["pathMatchers"][0]["pathRules"][0]["service"] = full
        self.assertFalse(module.merge_route(config, "sbx.sonmap.net", "/general/rsch/", uri("bes-rsch")))
        self.assertEqual(config, before)

    def test_host_scheme_gives_the_host_to_one_backend(self):
        config = {"defaultService": DEFAULT}
        self.assertTrue(module.merge_route(config, "rsch.general.sonmap.net", "/", uri("bes-rsch")))
        self.assertEqual(config["pathMatchers"][0]["defaultService"], uri("bes-rsch"))
        self.assertFalse(module.merge_route(config, "rsch.general.sonmap.net", "/", uri("bes-rsch")))

    def test_another_tasks_route_is_never_overwritten(self):
        config = {"defaultService": DEFAULT}
        module.merge_route(config, "sbx.sonmap.net", "/general/rsch/", uri("bes-rsch"))
        with self.assertRaisesRegex(RuntimeError, "already routed"):
            module.merge_route(config, "sbx.sonmap.net", "/general/rsch/", uri("bes-other"))
        module.merge_route(config, "rsch.general.sonmap.net", "/", uri("bes-rsch"))
        with self.assertRaisesRegex(RuntimeError, "already routed"):
            module.merge_route(config, "rsch.general.sonmap.net", "/", uri("bes-other"))


class BackendTests(unittest.TestCase):
    def test_backend_is_regional_internal_and_keeps_websockets_open(self):
        neg = {"name": "neg-1", "zone": REGION + "-a", "self_link": f"projects/{PROJECT}/zones/{REGION}-a/networkEndpointGroups/neg-1"}
        with mock.patch.object(module, "gcloud_describe_or_none", side_effect=[None, {"backends": []}]), \
                mock.patch.object(module, "gcloud", return_value="") as cloud:
            module.ensure_backend_service(PROJECT, "bes-sbx-rsch-jupyter", "hc-shared", [neg], ENV)
        calls = [call.args for call in cloud.call_args_list]
        create = next(args for args in calls if args[:3] == ("compute", "backend-services", "create"))
        self.assertIn("--load-balancing-scheme=INTERNAL_MANAGED", create)
        self.assertIn("--health-checks=hc-shared", create)
        self.assertIn("--timeout=86400s", create)
        for args in calls:
            self.assertIn("--region=" + REGION, args)
        self.assertTrue(any(args[:3] == ("compute", "backend-services", "add-backend") for args in calls))

    def test_attached_neg_is_not_detached_and_reattached(self):
        link = f"projects/{PROJECT}/zones/{REGION}-a/networkEndpointGroups/neg-1"
        neg = {"name": "neg-1", "zone": REGION + "-a", "self_link": link}
        current = {"loadBalancingScheme": "INTERNAL_MANAGED", "protocol": "HTTP",
                   "backends": [{"group": "https://www.googleapis.com/compute/v1/" + link}]}
        with mock.patch.object(module, "gcloud_describe_or_none", return_value=current), \
                mock.patch.object(module, "gcloud", return_value="") as cloud:
            module.ensure_backend_service(PROJECT, "bes-sbx-rsch-jupyter", "hc-shared", [neg], ENV)
        cloud.assert_not_called()

    def test_one_backend_per_container(self):
        request = make_request(migrated=True, containers=3)
        names = {module.backend_name(request["task"]["name"], c["name"]) for c in request["workload"]["containers"]}
        self.assertEqual(len(names), 3)

    def test_workload_identity_must_point_at_the_task_service_account(self):
        request = make_request(migrated=True)
        email = request["identity"]["jupyter_gsa"]["email"]
        with mock.patch.object(module, "run", return_value=email):
            module.verify_workload_identity(request, ENV)
        with mock.patch.object(module, "run", return_value="other@x.iam.gserviceaccount.com"):
            with self.assertRaisesRegex(RuntimeError, "KSA/GSA mismatch"):
                module.verify_workload_identity(request, ENV)


if __name__ == "__main__":
    unittest.main()
