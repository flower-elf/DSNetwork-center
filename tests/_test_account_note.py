import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_account(uid, note=None, **extra):
    data = {"uid": uid, "accessToken": "token-%s" % uid, "realm": "cn"}
    if note is not None:
        data["note"] = note
    data.update(extra)
    return wb_accounts.Account(data)


class NoteFieldTests(unittest.TestCase):
    def test_default_note_is_empty(self):
        self.assertEqual(make_account("n-def").note, "")

    def test_note_is_stripped_and_clamped(self):
        self.assertEqual(make_account("n-pad", note="  批量  ").note, "批量")
        long = "x" * 500
        self.assertEqual(len(make_account("n-long", note=long).note),
                         wb_accounts.MAX_ACCOUNT_NOTE)

    def test_junk_falls_back_to_empty(self):
        for junk in (None, 123, [], {}, True):
            self.assertEqual(make_account("n-junk", note=junk).note, "")

    def test_to_dict_and_public_carry_note(self):
        account = make_account("n-view", note="自用")
        self.assertEqual(account.to_dict()["note"], "自用")
        self.assertEqual(account.public()["note"], "自用")
        self.assertEqual(make_account("n-view2").public()["note"], "")

    def test_note_reaches_the_disk_through_both_write_paths(self):
        """Account.save 直写与 pool.set_note 各写一次，落盘重载后备注都可见。"""
        with tempfile.TemporaryDirectory(prefix="note-persist-") as directory:
            make_account("n-disk", note="批量01").save(directory)
            pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
            pool.accounts = [make_account("n-set")]
            pool.set_note("n-set", "批量02")
            loaded = {a.uid: a
                      for a in wb_accounts.AccountPool(directory, log=lambda _m: None).load()}
            self.assertEqual(loaded["n-disk"].note, "批量01")
            self.assertEqual(loaded["n-set"].note, "批量02")


class SetNoteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="note-set-")
        self.pool = wb_accounts.AccountPool(self.directory, log=lambda _m: None)
        self.pool.accounts = [make_account("s-a"), make_account("s-b", note="旧备注")]

    def test_sets_and_clears(self):
        updated = self.pool.set_note("s-a", "  批量  ")
        self.assertEqual(updated["note"], "批量")
        self.assertEqual(self.pool.set_note("s-a", "   ")["note"], "")
        self.assertEqual(self.pool.get("s-a").note, "")

    def test_overwrites_the_old_note(self):
        self.assertEqual(self.pool.set_note("s-b", "自用")["note"], "自用")
        self.assertEqual(self.pool.get("s-b").note, "自用")

    def test_unknown_uid_returns_none(self):
        self.assertIsNone(self.pool.set_note("nope", "x"))


if __name__ == "__main__":
    unittest.main()
