import math
import fcntl
import unittest
import uuid

from companion import apply_request, lock_path, validate_request


class CompanionTests(unittest.TestCase):
    def test_phone_locks_allow_two_phones_but_exclude_a_duplicate_worker(self):
        first_path, second_path = lock_path(str(uuid.uuid4())), lock_path(str(uuid.uuid4()))
        try:
            with open(first_path, "w") as first, open(second_path, "w") as second, open(first_path, "w") as duplicate:
                fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(duplicate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            first_path.unlink(missing_ok=True)
            second_path.unlink(missing_ok=True)

    def request(self, **changes):
        return {"version": 1, "id": str(uuid.uuid4()), "enabled": True,
                "latitude": 53.63, "longitude": 9.99, "name": "Hamburg", **changes}

    def test_selected_location_reaches_developer_device_tool(self):
        commands = []
        request = self.request(longitude=-3.218306)
        result = apply_request("phone", request, lambda *args: commands.append(args))
        self.assertTrue(result["success"])
        self.assertEqual(result["requestId"], request["id"])
        self.assertEqual(commands, [("phone", "simulate", "location", "coordinate",
                                     "--latitude", "53.63", "--longitude", "-3.218306")])

    def test_disable_clears_simulation(self):
        commands = []
        result = apply_request("phone", self.request(enabled=False), lambda *args: commands.append(args))
        self.assertTrue(result["success"])
        self.assertEqual(commands, [("phone", "simulate", "location", "clear")])

    def test_device_failure_is_reported_as_failure(self):
        def unavailable(*_args):
            raise RuntimeError("device disconnected")
        result = apply_request("phone", self.request(), unavailable)
        self.assertFalse(result["success"])
        self.assertIn("device disconnected", result["error"])

    def test_invalid_coordinates_never_reach_device(self):
        for changes in ({"latitude": 91}, {"longitude": -181}, {"latitude": math.nan},
                        {"longitude": True}, {"latitude": "53"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_request(self.request(**changes))

    def test_invalid_command_is_rejected(self):
        for changes in ({"version": 2}, {"enabled": "true"}, {"id": "not-a-uuid"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_request(self.request(**changes))


if __name__ == "__main__":
    unittest.main()
