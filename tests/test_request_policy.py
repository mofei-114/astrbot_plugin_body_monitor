import unittest

from request_policy import should_inject_health_data


class FakeEvent:
    def __init__(self, origin, message, *, group_id=None, proactive=False):
        self.unified_msg_origin = origin
        self.message_str = message
        self._group_id = group_id
        self.private_companion_proactive_framework = proactive

    def get_group_id(self):
        return self._group_id


class HealthDataInjectionPolicyTests(unittest.TestCase):
    def test_only_configured_private_explicit_health_requests_are_allowed(self):
        target = "aiocqhttp:FriendMessage:10001"
        configured = [target]

        self.assertTrue(
            should_inject_health_data(
                FakeEvent(target, "我的心率数据怎么样？"), configured
            )
        )
        self.assertTrue(
            should_inject_health_data(FakeEvent(target, "我今天心率高吗？"), configured)
        )
        self.assertTrue(
            should_inject_health_data(FakeEvent(target, "/body_status"), configured)
        )
        self.assertFalse(
            should_inject_health_data(FakeEvent(target, "今晚吃什么？"), configured)
        )
        self.assertFalse(
            should_inject_health_data(
                FakeEvent(target, "我的心率数据怎么样？", group_id="123"), configured
            )
        )
        self.assertFalse(
            should_inject_health_data(
                FakeEvent("aiocqhttp:FriendMessage:other", "我的心率数据怎么样？"),
                configured,
            )
        )
        non_private = "custom:ChannelMessage:10001"
        self.assertFalse(
            should_inject_health_data(
                FakeEvent(non_private, "我的心率数据怎么样？"), [non_private]
            )
        )
        self.assertFalse(
            should_inject_health_data(
                FakeEvent(target, "我的心率数据怎么样？", proactive=True), configured
            )
        )


if __name__ == "__main__":
    unittest.main()
