"""Security hardening tests for sandbox, fetch, and permission policy."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from firstcoder.permissions.policy import DefaultPermissionPolicy
from firstcoder.permissions.types import PermissionAction, PermissionMode, PermissionRequest
from firstcoder.tools.fetch import _is_private_or_local_url, _resolves_to_private_ip
from firstcoder.utils.execution_sandbox import ExecutionSandbox
from firstcoder.utils.sandbox_access import SandboxAccessMode


class TestEnvironmentFiltering:
    def test_passphrase_is_filtered(self, tmp_path):
        import os
        original = os.environ.get("GPG_PASSPHRASE")
        os.environ["GPG_PASSPHRASE"] = "secret"
        try:
            sandbox = ExecutionSandbox(tmp_path)
            env = sandbox.build_env()
            assert "GPG_PASSPHRASE" not in env
        finally:
            if original is None:
                os.environ.pop("GPG_PASSPHRASE", None)
            else:
                os.environ["GPG_PASSPHRASE"] = original

    def test_credential_is_filtered(self, tmp_path):
        import os
        original = os.environ.get("AWS_CREDENTIAL_PROFILE")
        os.environ["AWS_CREDENTIAL_PROFILE"] = "prod"
        try:
            sandbox = ExecutionSandbox(tmp_path)
            env = sandbox.build_env()
            assert "AWS_CREDENTIAL_PROFILE" not in env
        finally:
            if original is None:
                os.environ.pop("AWS_CREDENTIAL_PROFILE", None)
            else:
                os.environ["AWS_CREDENTIAL_PROFILE"] = original

    def test_non_sensitive_custom_vars_pass_through(self, tmp_path):
        import os
        original = os.environ.get("NORMAL_FLAG")
        os.environ["NORMAL_FLAG"] = "keep"
        try:
            sandbox = ExecutionSandbox(tmp_path)
            env = sandbox.build_env()
            assert env.get("NORMAL_FLAG") == "keep"
        finally:
            if original is None:
                os.environ.pop("NORMAL_FLAG", None)
            else:
                os.environ["NORMAL_FLAG"] = original


class TestAggressiveMode:
    def _request(self, command: str) -> PermissionRequest:
        return PermissionRequest(
            id="test-request",
            action=PermissionAction.EXECUTE_SHELL,
            target=command,
            reason="test",
        )

    def _policy(self, tmp_path):
        return DefaultPermissionPolicy(tmp_path)

    def test_pytest_requires_confirmation(self, tmp_path):
        policy = self._policy(tmp_path)
        decision = policy.decide(self._request("pytest"), mode=PermissionMode.AGGRESSIVE)
        assert decision.kind.name == "ASK"

    def test_make_test_requires_confirmation(self, tmp_path):
        policy = self._policy(tmp_path)
        decision = policy.decide(self._request("make test"), mode=PermissionMode.AGGRESSIVE)
        assert decision.kind.name == "ASK"

    def test_cargo_test_requires_confirmation(self, tmp_path):
        policy = self._policy(tmp_path)
        decision = policy.decide(self._request("cargo test"), mode=PermissionMode.AGGRESSIVE)
        assert decision.kind.name == "ASK"

    def test_ruff_still_auto_allowed(self, tmp_path):
        policy = self._policy(tmp_path)
        decision = policy.decide(self._request("ruff check ."), mode=PermissionMode.AGGRESSIVE)
        assert decision.kind.name == "ALLOW"

    def test_git_status_still_auto_allowed(self, tmp_path):
        policy = self._policy(tmp_path)
        decision = policy.decide(self._request("git status"), mode=PermissionMode.AGGRESSIVE)
        assert decision.kind.name == "ALLOW"


class TestSSRFProtection:
    def test_localhost_url_is_rejected(self):
        from urllib.parse import urlparse
        assert _is_private_or_local_url(urlparse("http://localhost/x"))

    def test_private_ip_url_is_rejected(self):
        from urllib.parse import urlparse
        assert _is_private_or_local_url(urlparse("http://10.0.0.1/x"))

    def test_public_ip_url_is_allowed(self):
        from urllib.parse import urlparse
        assert not _is_private_or_local_url(urlparse("http://8.8.8.8/x"))

    def test_hostname_resolving_to_private_ip_detected(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            import socket
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
            ]
            assert _resolves_to_private_ip("rebind.example.com")

    def test_hostname_resolving_to_public_ip_allowed(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            import socket
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            ]
            assert not _resolves_to_private_ip("example.com")

    def test_unresolvable_hostname_treated_as_private(self):
        with patch("socket.getaddrinfo", side_effect=OSError("DNS failure")):
            assert _resolves_to_private_ip("nonexistent.invalid")
