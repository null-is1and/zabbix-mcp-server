#
# Zabbix MCP Server
# Copyright (C) 2026 initMAX s.r.o.
# Licensed under the GNU Affero General Public License v3.
# See LICENSE for details.
#

"""source_file must be a regular file, and errors must say what happened.

Follow-up to the #81 review: containment plus O_NOFOLLOW let a
directory, FIFO or device under an allowed directory through to the
read, and every open failure was reported as "must not be a symbolic
link" - including a plain missing file.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from zabbix_mcp.server import _resolve_source_file


class TestRegularFileOnly(unittest.TestCase):

    def test_directory_under_allowed_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = os.path.join(tmp, "sub")
            os.mkdir(sub)
            with self.assertRaises(ValueError) as ctx:
                _resolve_source_file({"source_file": sub}, allowed_import_dirs=[tmp])
            self.assertIn("regular file", str(ctx.exception))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs not supported here")
    def test_fifo_under_allowed_dir_is_refused_and_does_not_block(self):
        # No writer on the other end: without O_NONBLOCK the open itself
        # would park the thread forever, and this test would hang on
        # the old code rather than fail.
        with tempfile.TemporaryDirectory() as tmp:
            fifo = os.path.join(tmp, "pipe.yaml")
            os.mkfifo(fifo)
            with self.assertRaises(ValueError) as ctx:
                _resolve_source_file({"source_file": fifo}, allowed_import_dirs=[tmp])
            self.assertIn("regular file", str(ctx.exception))

    def test_missing_file_is_not_called_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "nope.yaml")
            with self.assertRaises(ValueError) as ctx:
                _resolve_source_file({"source_file": missing}, allowed_import_dirs=[tmp])
            self.assertNotIn("symbolic link", str(ctx.exception))
            self.assertIn("could not be opened", str(ctx.exception))

    def test_regular_file_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = os.path.join(tmp, "t.yaml")
            with open(f, "w") as fh:
                fh.write("zabbix_export: {}\n")
            result = _resolve_source_file({"source_file": f}, allowed_import_dirs=[tmp])
            self.assertIn("zabbix_export", result["source"])
            self.assertEqual(result["format"], "yaml")


if __name__ == "__main__":
    unittest.main()
