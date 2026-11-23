import json
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from api.urls import urlpatterns


class LoginRequiredTests(TestCase):
    def test_all_business_routes_reject_anonymous_requests(self):
        public = {"health/", "auth/login/", "auth/me/", "mcp/"}
        for route in urlpatterns:
            path = str(route.pattern)
            if path in public:
                continue
            path = re.sub(r"<uuid:[^>]+>", "00000000-0000-0000-0000-000000000001", path)
            path = re.sub(r"<[^>]+>", "1", path)
            for method in ("get", "post", "put", "patch", "delete"):
                with self.subTest(path=path, method=method):
                    response = getattr(self.client, method)("/api/" + path)
                    self.assertEqual(response.status_code, 401)
                    self.assertTrue(response.json()["needLogin"])

    def test_login_regular_access_admin_restriction_and_logout(self):
        get_user_model().objects.create_user("reader", password="test-password")
        self.assertFalse(self.client.get("/api/auth/me/").json()["authenticated"])
        self.assertEqual(self.client.post("/api/auth/login/", data=json.dumps({
            "username": "reader", "password": "wrong"
        }), content_type="application/json").status_code, 401)
        response = self.client.post("/api/auth/login/", data=json.dumps({
            "username": "reader", "password": "test-password"
        }), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["isAdmin"])
        with patch("api.views.list_projects", return_value=[]):
            self.assertEqual(self.client.get("/api/projects/").status_code, 200)
        self.assertEqual(self.client.post("/api/ingestion/build/").status_code, 403)
        self.assertEqual(self.client.post("/api/auth/logout/").status_code, 200)
        self.assertEqual(self.client.get("/api/projects/").status_code, 401)

    def test_preflight_still_works(self):
        self.assertEqual(self.client.options("/api/projects/").status_code, 200)

    def test_health_is_public(self):
        response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["portal"], "ok")
