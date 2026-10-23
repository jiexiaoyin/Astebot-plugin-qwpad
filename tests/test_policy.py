from types import SimpleNamespace

from qwpad_plugin.qwpad_policy import PolicySnapshot, classify


def _msg(chat_type="dm", user_id="u1", chat_id="u1", mentions=False):
    return SimpleNamespace(
        chat_type=chat_type,
        user_id=user_id,
        chat_id=chat_id,
        mentions_me=mentions,
    )


def test_dm_denied_by_default():
    policy = PolicySnapshot()
    assert not classify(policy, _msg())


def test_dm_whitelisted():
    policy = PolicySnapshot(allow_users=frozenset({"u1"}))
    assert classify(policy, _msg())


def test_allow_all_users():
    policy = PolicySnapshot(allow_all_users=True)
    assert classify(policy, _msg())


def test_block_beats_allow():
    policy = PolicySnapshot(
        allow_users=frozenset({"u1"}), block_users=frozenset({"u1"})
    )
    decision = classify(policy, _msg())
    assert not decision
    assert decision.reason == "blocked_user"


def test_group_atbot_requires_mention():
    policy = PolicySnapshot(allow_groups=frozenset({"room-1"}))
    assert not classify(policy, _msg("group", "u1", "room-1", mentions=False))
    assert classify(policy, _msg("group", "u1", "room-1", mentions=True))


def test_group_all_admits_without_mention():
    policy = PolicySnapshot(allow_groups=frozenset({"room-1"}), group_reply="all")
    assert classify(policy, _msg("group", "u1", "room-1", mentions=False))


def test_group_none_ignores():
    policy = PolicySnapshot(allow_groups=frozenset({"room-1"}), group_reply="none")
    assert not classify(policy, _msg("group", "u1", "room-1", mentions=True))


def test_group_member_whitelist_applies():
    policy = PolicySnapshot(allow_users=frozenset({"u1"}))
    assert classify(policy, _msg("group", "u1", "other-room", mentions=True))


def test_group_blocked():
    policy = PolicySnapshot(allow_all_users=True, block_groups=frozenset({"room-1"}))
    assert not classify(policy, _msg("group", "u1", "room-1", mentions=True))


def test_from_config_csv_and_mode():
    policy = PolicySnapshot.from_config(
        {
            "allow_users": "u1, u2",
            "allow_groups": ["room-1"],
            "group_reply": "ALL",
            "allow_all_users": "true",
        }
    )
    assert policy.allow_users == frozenset({"u1", "u2"})
    assert policy.allow_groups == frozenset({"room-1"})
    assert policy.group_reply == "all"
    assert policy.allow_all_users is True
