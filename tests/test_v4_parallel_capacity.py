import unittest

from vibe_guide.dag import schedule_ready_nodes


class V4ParallelCapacityTests(unittest.TestCase):
    def test_capacity_is_capped_at_five_and_queue_refills(self):
        nodes = [{"id": "n%d" % i, "status": "ready", "developer_task_id": "d%d" % i,
                  "reviewer_task_id": "r%d" % i} for i in range(7)]
        first = schedule_ready_nodes(nodes, active_pairs=0)
        self.assertEqual(first, ["n0", "n1", "n2", "n3", "n4"])
        self.assertEqual(sum(node["status"] == "running" for node in nodes), 5)
        nodes[0]["status"] = "archived"
        refill = schedule_ready_nodes(nodes, active_pairs=4)
        self.assertEqual(refill, ["n5"])

    def test_isolated_node_does_not_lock_other_ready_nodes(self):
        nodes = [{"id": "isolated", "status": "ready", "isolated": True},
                 {"id": "free", "status": "ready", "developer_task_id": "d-free", "reviewer_task_id": "r-free"}]
        self.assertEqual(schedule_ready_nodes(nodes), ["free"])

    def test_duplicate_writer_is_rejected_before_start(self):
        nodes = [
            {"id": "active", "status": "running", "writer": "writer-1"},
            {"id": "next", "status": "ready", "writer": "writer-1",
             "developer_task_id": "d-next", "reviewer_task_id": "r-next"},
        ]
        with self.assertRaises(ValueError):
            schedule_ready_nodes(nodes, active_pairs=1)

    def test_same_developer_and_reviewer_are_rejected(self):
        nodes = [{"id": "bad", "status": "ready", "developer_task_id": "task-1",
                  "reviewer_task_id": "task-1"}]
        with self.assertRaises(ValueError):
            schedule_ready_nodes(nodes)

    def test_missing_task_identity_is_rejected(self):
        nodes = [{"id": "missing", "status": "ready"}]
        with self.assertRaises(ValueError):
            schedule_ready_nodes(nodes)

    def test_duplicate_writer_batch_is_atomic(self):
        nodes = [
            {"id": "first", "status": "ready", "writer": "shared",
             "developer_task_id": "d1", "reviewer_task_id": "r1"},
            {"id": "second", "status": "ready", "writer": "shared",
             "developer_task_id": "d2", "reviewer_task_id": "r2"},
        ]
        with self.assertRaises(ValueError):
            schedule_ready_nodes(nodes)
        self.assertEqual([node["status"] for node in nodes], ["ready", "ready"])

    def test_invalid_queued_node_does_not_block_first_five(self):
        nodes = [
            {"id": "n%d" % i, "status": "ready", "developer_task_id": "d%d" % i,
             "reviewer_task_id": "r%d" % i}
            for i in range(6)
        ]
        nodes[5]["developer_task_id"] = None
        self.assertEqual(schedule_ready_nodes(nodes, active_pairs=0),
                         ["n0", "n1", "n2", "n3", "n4"])
        self.assertEqual(nodes[5]["status"], "ready")


if __name__ == "__main__":
    unittest.main()
