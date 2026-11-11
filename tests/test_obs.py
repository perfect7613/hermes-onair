"""The OBS client: handshake, correlation, failure and recovery."""

from __future__ import annotations

import threading
import time
import unittest

from onair.core.errors import ObsAuthError, ObsRequestError, ObsTimeout, ObsUnavailable
from onair.core.obs import Endpoint, ObsClient, auth_response

from .fake_obs import FakeObs


def wait_for(condition, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class ObsClientTests(unittest.TestCase):
    def make(self, obs: FakeObs, password: str = "") -> ObsClient:
        client = ObsClient(lambda: Endpoint(password=password), obs.connector, connect_timeout=1.0)
        self.addCleanup(client.close)
        return client

    def test_auth_response_matches_the_protocol_reference_shape(self):
        # Deterministic, 44 chars of base64 for a SHA-256 digest, and sensitive to every input.
        value = auth_response("secret", "salt", "challenge")
        self.assertEqual(len(value), 44)
        self.assertEqual(value, auth_response("secret", "salt", "challenge"))
        self.assertNotEqual(value, auth_response("secret2", "salt", "challenge"))
        self.assertNotEqual(value, auth_response("secret", "salt", "other"))

    def test_nothing_connects_until_the_first_call(self):
        obs = FakeObs()
        client = self.make(obs)
        self.assertEqual(obs.connect_count, 0)
        self.assertFalse(client.connected)
        self.assertEqual(client.call("GetVersion")["obsVersion"], "33.0.1")
        self.assertEqual(obs.connect_count, 1)
        client.call("GetVersion")
        self.assertEqual(obs.connect_count, 1, "the connection is reused")

    def test_password_is_accepted_and_a_wrong_one_is_named(self):
        obs = FakeObs(password="hunter2")
        self.assertTrue(self.make(obs, "hunter2").call("GetVersion"))
        with self.assertRaises(ObsAuthError) as wrong:
            self.make(obs, "nope").call("GetVersion")
        self.assertIn("password", wrong.exception.hint.lower())
        with self.assertRaises(ObsAuthError):
            self.make(obs, "").call("GetVersion")

    def test_obs_not_running_is_reported_with_a_fix(self):
        obs = FakeObs()
        obs.down = True
        with self.assertRaises(ObsUnavailable) as failure:
            self.make(obs).call("GetVersion")
        self.assertIn("WebSocket", failure.exception.hint)

    def test_a_refused_request_carries_the_obs_code(self):
        with self.assertRaises(ObsRequestError) as failure:
            self.make(FakeObs()).call("SetCurrentProgramScene", {"sceneName": "Nope"})
        self.assertEqual(failure.exception.code, 600)

    def test_batch_reports_each_request_separately(self):
        results = self.make(FakeObs()).call_batch([("GetVersion", {}), ("SetCurrentProgramScene", {"sceneName": "Nope"})])
        self.assertEqual(results[0]["platform"], "macos")
        self.assertIsInstance(results[1], ObsRequestError)

    def test_concurrent_calls_get_their_own_answers(self):
        obs = FakeObs()
        client = self.make(obs)
        answers: list[str] = []

        def worker(index: int) -> None:
            request = "GetVersion" if index % 2 else "GetCurrentProgramScene"
            answers.append("obsVersion" if "obsVersion" in client.call(request) else "scene")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(answers), ["obsVersion"] * 10 + ["scene"] * 10)
        self.assertEqual(obs.connect_count, 1)

    def test_a_dropped_connection_is_replaced_on_the_next_call(self):
        obs = FakeObs()
        client = self.make(obs)
        client.call("GetVersion")
        obs.drop_connections()
        self.assertTrue(wait_for(lambda: not client.connected))
        self.assertTrue(client.call("GetVersion"))
        self.assertEqual(obs.connect_count, 2)

    def test_a_call_waiting_when_the_socket_dies_fails_fast(self):
        obs = FakeObs()
        client = self.make(obs)
        client.call("GetVersion")
        obs.mute = True
        errors: list[Exception] = []
        thread = threading.Thread(target=lambda: self._capture(errors, lambda: client.call("GetVersion", timeout=5)))
        thread.start()
        time.sleep(0.1)
        obs.drop_connections()
        thread.join(2)
        self.assertFalse(thread.is_alive(), "the caller must not wait out the full timeout")
        self.assertIsInstance(errors[0], ObsUnavailable)

    def test_silence_is_a_timeout(self):
        obs = FakeObs()
        client = self.make(obs)
        client.call("GetVersion")
        obs.mute = True
        with self.assertRaises(ObsTimeout):
            client.call("GetVersion", timeout=0.1)

    def test_events_reach_subscribers_and_can_call_back(self):
        obs = FakeObs()
        client = self.make(obs)
        seen: list[str] = []
        client.subscribe("ReplayBufferSaved",
                         lambda event: seen.append(event["savedReplayPath"] + client.call("GetVersion")["platform"]))
        client.call("GetVersion")
        obs.press_save_replay_hotkey()
        self.assertTrue(wait_for(lambda: seen))
        self.assertEqual(seen, ["/videos/Replay 1.mkvmacos"])

    def test_unsubscribe_stops_delivery(self):
        obs = FakeObs()
        client = self.make(obs)
        seen: list[dict] = []
        unsubscribe = client.subscribe("ReplayBufferSaved", seen.append)
        client.call("GetVersion")
        unsubscribe()
        obs.press_save_replay_hotkey()
        time.sleep(0.1)
        self.assertEqual(seen, [])

    def test_a_held_connection_comes_back_by_itself(self):
        obs = FakeObs()
        client = self.make(obs)
        client.call("GetVersion")
        client.hold(True)
        obs.drop_connections()
        self.assertTrue(wait_for(lambda: obs.connect_count == 2 and client.connected, timeout=4))

    def test_close_fails_later_calls_instead_of_reconnecting(self):
        obs = FakeObs()
        client = self.make(obs)
        client.call("GetVersion")
        client.close()
        with self.assertRaises(ObsUnavailable):
            client.call("GetVersion")
        self.assertEqual(obs.connect_count, 1)

    @staticmethod
    def _capture(errors: list, action) -> None:
        try:
            action()
        except Exception as exc:
            errors.append(exc)


if __name__ == "__main__":
    unittest.main()
