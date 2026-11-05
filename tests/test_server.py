"""End-to-end tests for the Phone2Mac server, using an in-memory clipboard.

Run:  python3 -m unittest discover -s tests -v
"""
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import phone2mac  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
JPEG = b"\xff\xd8\xff\xe0" + os.urandom(5000)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.clip = phone2mac.MemoryClipboard()
        self.port = free_port()
        self.app = phone2mac.App(root / "data", root / "inbox", self.port, self.clip)
        self.server = phone2mac.make_server(self.app, "")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.k = self.app.token

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    # -- helpers --
    def req(self, method, path, body=None, headers=None, auth=True, host="127.0.0.1"):
        if auth:
            path += ("&" if "?" in path else "?") + "k=" + self.k
        c = http.client.HTTPConnection(host, self.port, timeout=30)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def items(self):
        r, d = self.req("GET", "/api/items")
        self.assertEqual(r.status, 200)
        return json.loads(d)["items"]

    # -- tests --
    def test_auth_required(self):
        r, _ = self.req("GET", "/api/items", auth=False)
        self.assertEqual(r.status, 401)
        r, _ = self.req("GET", "/api/items?k=wrong", auth=False)
        self.assertEqual(r.status, 401)
        r, _ = self.req("GET", "/api/items", auth=False, headers={"X-Token": self.k})
        self.assertEqual(r.status, 200)
        r, _ = self.req("GET", "/api/items", auth=False, headers={"Authorization": "Bearer " + self.k})
        self.assertEqual(r.status, 200)

    def test_index_and_icon(self):
        r, d = self.req("GET", "/", auth=False)
        self.assertEqual(r.status, 200)
        self.assertIn(b"Phone2Mac", d)
        r, d = self.req("GET", "/icon.png", auth=False)
        self.assertEqual(r.status, 200)
        self.assertTrue(d.startswith(b"\x89PNG"))

    def test_pair_only_on_localhost(self):
        r, d = self.req("GET", "/pair", auth=False)
        self.assertEqual(r.status, 200)
        self.assertIn(self.k.encode(), d)
        # DNS-rebinding style request: loopback IP but foreign Host header
        r, _ = self.req("GET", "/pair", auth=False, headers={"Host": "evil.example.com"})
        self.assertEqual(r.status, 403)

    def test_send_text_json(self):
        r, d = self.req("POST", "/api/send", json.dumps({"text": "héllo wörld 👋"}),
                        {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (iPhone)"})
        self.assertEqual(r.status, 200, d)
        self.assertEqual(self.clip.writes[-1], ("text", "héllo wörld 👋"))
        it = self.items()[0]
        self.assertEqual((it["kind"], it["source"], it["device"], it["text"]), ("text", "phone", "iPhone", "héllo wörld 👋"))

    def test_send_link_raw_text(self):
        r, _ = self.req("POST", "/api/send", "  https://example.com/a?b=1 \n".encode(), {"Content-Type": "text/plain"})
        self.assertEqual(r.status, 200)
        it = self.items()[0]
        self.assertEqual(it["kind"], "link")
        self.assertEqual(it["text"], "https://example.com/a?b=1")
        self.assertEqual(self.clip.writes[-1], ("text", "https://example.com/a?b=1"))

    def test_send_raw_text_without_content_type(self):
        # e.g. a Shortcut that sends text as an untyped body
        r, _ = self.req("POST", "/api/send", "plain words".encode(), {"Content-Type": "application/octet-stream"})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.items()[0]["text"], "plain words")

    def test_send_form_urlencoded(self):
        r, _ = self.req("POST", "/api/send", "text=a%20b%26c", {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.items()[0]["text"], "a b&c")

    def test_upload_raw_file_with_name(self):
        name = urllib.parse.quote("My Photo é.jpg")
        r, d = self.req("POST", "/api/send", JPEG, {"Content-Type": "image/jpeg", "X-Filename": name})
        self.assertEqual(r.status, 200, d)
        it = self.items()[0]
        self.assertEqual((it["kind"], it["name"], it["size"]), ("image", "My Photo é.jpg", len(JPEG)))
        saved = Path(self.app.inbox, "My Photo é.jpg")
        self.assertEqual(saved.read_bytes(), JPEG)
        self.assertEqual(self.clip.writes[-1], ("files", [str(saved)]))
        # download it back
        r, d = self.req("GET", f"/api/file/{it['id']}")
        self.assertEqual((r.status, d), (200, JPEG))
        self.assertEqual(r.getheader("Content-Type"), "image/jpeg")

    def test_upload_unnamed_binary_is_sniffed(self):
        r, _ = self.req("POST", "/api/send", PNG, {"Content-Type": "application/octet-stream"})
        self.assertEqual(r.status, 200)
        it = self.items()[0]
        self.assertEqual(it["kind"], "image")
        self.assertTrue(it["name"].startswith("Photo ") and it["name"].endswith(".png"), it["name"])

    def test_duplicate_names_dont_overwrite(self):
        for _ in range(2):
            self.req("POST", "/api/send", b"%PDF-1.4 data", {"Content-Type": "application/pdf", "X-Filename": "doc.pdf"})
        names = sorted(i["name"] for i in self.items())
        self.assertEqual(names, ["doc (2).pdf", "doc.pdf"])

    def test_path_traversal_name_is_sanitised(self):
        self.req("POST", "/api/send", b"%PDF-1.4", {"Content-Type": "application/pdf", "X-Filename": "../../etc/passwd.pdf"})
        it = self.items()[0]
        self.assertEqual(it["name"], "passwd.pdf")
        self.assertTrue(Path(self.app.inbox, "passwd.pdf").exists())

    def test_batch_puts_all_files_on_clipboard(self):
        for n in ("a.pdf", "b.pdf", "c.pdf"):
            self.req("POST", "/api/send", b"%PDF", {"Content-Type": "application/pdf", "X-Filename": n, "X-Batch": "xyz"})
        kind, paths = self.clip.writes[-1]
        self.assertEqual([Path(p).name for p in paths], ["a.pdf", "b.pdf", "c.pdf"])

    def test_multipart_files_and_text(self):
        boundary = "----p2mBoundary7MA4YWxk"
        big = os.urandom(700_000) + b"\r\n--" + b"x" * 10  # contains near-boundary bytes
        parts = [
            (b'Content-Disposition: form-data; name="file"; filename="clip.bin"\r\n'
             b"Content-Type: application/octet-stream\r\n\r\n" + big),
            (b'Content-Disposition: form-data; name="file2"; filename="pic.png"\r\n'
             b"Content-Type: image/png\r\n\r\n" + PNG),
        ]
        body = b"".join(b"--" + boundary.encode() + b"\r\n" + p + b"\r\n" for p in parts) + b"--" + boundary.encode() + b"--\r\n"
        r, d = self.req("POST", "/api/send", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        self.assertEqual(r.status, 200, d)
        self.assertEqual(Path(self.app.inbox, "clip.bin").read_bytes(), big)
        self.assertEqual(Path(self.app.inbox, "pic.png").read_bytes(), PNG)
        self.assertEqual(len(self.clip.writes[-1][1]), 2)

    def test_multipart_text_field(self):
        boundary = "b0undary"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\nfrom a form ✓\r\n--{boundary}--\r\n").encode()
        r, _ = self.req("POST", "/api/send", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.clip.writes[-1], ("text", "from a form ✓"))

    def test_chunked_upload(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        data = os.urandom(300_000)
        c.request("POST", f"/api/send?k={self.k}", body=iter([data[:100_000], data[100_000:]]),
                  headers={"Content-Type": "application/zip", "X-Filename": "big.zip"}, encode_chunked=True)
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 200)
        self.assertEqual(Path(self.app.inbox, "big.zip").read_bytes(), data)

    def test_keepalive_multiple_requests_one_connection(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        for i in range(3):
            c.request("POST", f"/api/send?k={self.k}", body=json.dumps({"text": f"n{i}"}), headers={"Content-Type": "application/json"})
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 200)
        c.request("GET", f"/api/items?k={self.k}")
        items = json.loads(c.getresponse().read())["items"]
        self.assertEqual([i["text"] for i in items[:3]], ["n2", "n1", "n0"])

    def test_mac_clipboard_capture_and_dedupe(self):
        self.app.on_clip_event({"event": "change", "kind": "text", "text": "copied on mac"})
        self.app.on_clip_event({"event": "change", "kind": "text", "text": "second"})
        self.app.on_clip_event({"event": "change", "kind": "text", "text": "copied on mac"})  # re-copy => bump, not dup
        items = self.items()
        self.assertEqual([i["text"] for i in items], ["copied on mac", "second"])
        self.assertEqual(items[0]["source"], "mac")
        # Latest endpoint mirrors the top of the history (the Mac clipboard)
        r, d = self.req("GET", "/api/latest")
        self.assertEqual((r.status, d.decode()), (200, "copied on mac"))

    def test_mac_image_and_files_capture(self):
        clip_png = Path(self.tmp.name, "clip-1.png")
        clip_png.write_bytes(PNG)
        self.app.on_clip_event({"event": "change", "kind": "image", "path": str(clip_png)})
        it = self.items()[0]
        self.assertEqual(it["kind"], "image")
        r, d = self.req("GET", "/api/latest")
        self.assertEqual(d, PNG)
        self.assertIn("attachment", r.getheader("Content-Disposition"))

        folder = Path(self.tmp.name, "My Folder")
        folder.mkdir()
        (folder / "x.txt").write_text("hi")
        doc = Path(self.tmp.name, "report.pdf")
        doc.write_bytes(b"%PDF-1.7")
        self.app.on_clip_event({"event": "change", "kind": "files", "paths": [str(doc), str(folder)]})
        items = self.items()
        self.assertEqual([(i["kind"], i["name"]) for i in items[:2]], [("file", "report.pdf"), ("folder", "My Folder")])
        r, d = self.req("GET", f"/api/file/{items[1]['id']}")
        self.assertEqual(r.status, 200)
        self.assertTrue(d.startswith(b"PK"))
        self.assertIn("My%20Folder.zip", r.getheader("Content-Disposition"))

    def test_range_requests(self):
        data = bytes(range(256)) * 40
        self.req("POST", "/api/send", data, {"Content-Type": "video/mp4", "X-Filename": "v.mp4"})
        it = self.items()[0]
        r, d = self.req("GET", f"/api/file/{it['id']}", headers={"Range": "bytes=100-199"})
        self.assertEqual((r.status, d), (206, data[100:200]))
        self.assertEqual(r.getheader("Content-Range"), f"bytes 100-199/{len(data)}")
        r, d = self.req("GET", f"/api/file/{it['id']}", headers={"Range": "bytes=-10"})
        self.assertEqual(d, data[-10:])
        r, _ = self.req("GET", f"/api/file/{it['id']}", headers={"Range": "bytes=999999-"})
        self.assertEqual(r.status, 416)

    def test_copy_delete_clear(self):
        self.req("POST", "/api/send", json.dumps({"text": "one"}), {"Content-Type": "application/json"})
        self.req("POST", "/api/send", json.dumps({"text": "two"}), {"Content-Type": "application/json"})
        one = self.items()[1]
        r, _ = self.req("POST", f"/api/copy/{one['id']}")
        self.assertEqual(r.status, 200)
        self.assertEqual(self.clip.writes[-1], ("text", "one"))
        self.assertEqual(self.items()[0]["id"], one["id"])
        r, _ = self.req("POST", f"/api/delete/{one['id']}")
        self.assertEqual([i["text"] for i in self.items()], ["two"])
        self.req("POST", "/api/clear")
        self.assertEqual(self.items(), [])

    def test_long_poll_wakes_on_change(self):
        r, d = self.req("GET", "/api/items")
        v = json.loads(d)["version"]
        result = {}

        def waiter():
            t = time.time()
            r, d = self.req("GET", f"/api/items?v={v}&wait=1")
            result["elapsed"] = time.time() - t
            result["items"] = json.loads(d)["items"]

        th = threading.Thread(target=waiter)
        th.start()
        time.sleep(0.5)
        self.app.on_clip_event({"event": "change", "kind": "text", "text": "wake up"})
        th.join(10)
        self.assertLess(result["elapsed"], 5)
        self.assertEqual(result["items"][0]["text"], "wake up")

    def test_history_persists_across_restart(self):
        self.req("POST", "/api/send", json.dumps({"text": "persist me"}), {"Content-Type": "application/json"})
        app2 = phone2mac.App(self.app.data_dir, self.app.inbox, self.port, phone2mac.MemoryClipboard())
        self.assertEqual(app2.store.top()["text"], "persist me")
        self.assertEqual(app2.token, self.app.token)

    def test_history_is_capped(self):
        for i in range(phone2mac.MAX_ITEMS + 5):
            self.app.on_clip_event({"event": "change", "kind": "text", "text": f"item {i}"})
        self.assertEqual(len(self.items()), phone2mac.MAX_ITEMS)

    def test_shortcut_text_labelled_without_extension_is_text(self):
        r, _ = self.req("POST", "/api/send", "from shortcut".encode(),
                        {"Content-Type": "application/octet-stream", "X-Filename": "Text"})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.items()[0]["kind"], "text")

    def test_rich_text_clipboard_becomes_plain_text(self):
        html = b"<meta charset='utf-8'><p>Hello <b>bold</b></p><p>world</p>"
        r, _ = self.req("POST", "/api/send", html, {"Content-Type": "text/html"})
        self.assertEqual(r.status, 200)
        it = self.items()[0]
        self.assertEqual(it["kind"], "text")
        self.assertEqual(it["text"], "Hello bold\nworld")
        rtf = rb"{\rtf1\ansi\ansicpg1252{\fonttbl\f0\fswiss Helvetica;}\f0\fs24 Hi from WhatsApp\par second line}"
        r, _ = self.req("POST", "/api/send", rtf, {"Content-Type": "application/octet-stream"})
        self.assertEqual(r.status, 200)
        it = self.items()[0]
        self.assertEqual(it["kind"], "text")
        self.assertIn("Hi from WhatsApp", it["text"])
        self.assertNotIn("\\", it["text"])
        # a real .rtf document shared on purpose stays a file
        self.req("POST", "/api/send", rtf, {"Content-Type": "application/rtf", "X-Filename": "notes.rtf"})
        self.assertEqual(self.items()[0]["kind"], "file")

    def test_shortcut_reply_text(self):
        r, d = self.req("POST", "/api/send?reply=text", json.dumps({"text": "x"}), {"Content-Type": "application/json"})
        self.assertEqual(d, b"Sent to Mac")

    def test_shortcut_download_needs_mac(self):
        if sys.platform == "darwin":
            self.skipTest("would really sign")
        r, d = self.req("GET", "/api/shortcut/send?host=my-mac.local:8765")
        self.assertEqual(r.status, 501)
        r, _ = self.req("GET", "/api/shortcut/send", auth=False)
        self.assertEqual(r.status, 401)

    def test_universal_clipboard_overwrite_is_reverted(self):
        self.req("POST", "/api/send", json.dumps({"text": "keep me"}), {"Content-Type": "application/json"})
        n = len(self.clip.writes)
        self.app.on_remote_clipboard()  # Apple's Universal Clipboard clobbers it moments later
        self.assertEqual(self.clip.writes[n:], [("text", "keep me")])
        # Long after our write, Apple's clipboard is left alone
        self.app.last_write = (time.time() - 120,) + self.app.last_write[1:]
        self.app.on_remote_clipboard()
        self.assertEqual(len(self.clip.writes), n + 1)
        # A remote event never becomes a history item
        self.app.on_clip_event({"event": "change", "kind": "remote"})
        self.assertEqual(len(self.items()), 1)

    def test_empty_send_rejected(self):
        r, _ = self.req("POST", "/api/send", b"", {"Content-Type": "text/plain"})
        self.assertEqual(r.status, 400)


class UnitTest(unittest.TestCase):
    def test_safe_name(self):
        self.assertEqual(phone2mac.safe_name("../a/b/c.txt"), "c.txt")
        self.assertEqual(phone2mac.safe_name("..\\..\\x.png"), "x.png")
        self.assertEqual(phone2mac.safe_name("...", "d"), "d")
        self.assertEqual(phone2mac.safe_name(".hidden"), "hidden")

    def test_text_kind(self):
        self.assertEqual(phone2mac.text_kind("https://a.b/c"), "link")
        self.assertEqual(phone2mac.text_kind(" www.x.com "), "link")
        self.assertEqual(phone2mac.text_kind("see https://a.b"), "text")

    def test_build_shortcuts(self):
        import plistlib
        for kind in ("send", "get", "share"):
            wf = phone2mac.build_shortcut(kind, "http://mac.local:8765", "tok/en+")
            raw = plistlib.dumps(wf, fmt=plistlib.FMT_BINARY)
            back = plistlib.loads(raw)
            urls = [a["WFWorkflowActionParameters"].get("WFURL") for a in back["WFWorkflowActions"]]
            urls = [u for u in urls if u]
            self.assertTrue(urls and all(u.startswith("http://mac.local:8765/api/") and "k=tok%2Fen%2B" in u for u in urls), urls)
        simple = phone2mac.build_shortcut("send", "http://h", "t")
        self.assertEqual([a["WFWorkflowActionIdentifier"].split(".")[-1] for a in simple["WFWorkflowActions"]],
                         ["getclipboard", "downloadurl", "notification"])
        dl = simple["WFWorkflowActions"][1]["WFWorkflowActionParameters"]
        self.assertEqual(dl["WFRequestVariable"]["Value"]["OutputUUID"],
                         simple["WFWorkflowActions"][0]["WFWorkflowActionParameters"]["UUID"])
        send = phone2mac.build_shortcut("share", "http://h", "t")
        self.assertEqual(send["WFWorkflowNoInputBehavior"]["Name"], "WFWorkflowNoInputBehaviorGetClipboard")
        self.assertIn("ActionExtension", send["WFWorkflowTypes"])
        # control-flow blocks are balanced
        groups = {}
        for a in send["WFWorkflowActions"]:
            p = a["WFWorkflowActionParameters"]
            if "GroupingIdentifier" in p:
                groups.setdefault(p["GroupingIdentifier"], []).append(p["WFControlFlowMode"])
        for modes in groups.values():
            self.assertEqual((modes[0], modes[-1]), (0, 2))

    def test_sniff(self):
        self.assertEqual(phone2mac.sniff(b"\x00\x00\x00\x18ftypheic")[0], "image/heic")
        self.assertEqual(phone2mac.sniff(b"\x00\x00\x00\x14ftypqt  ")[0], "video/quicktime")
        self.assertIsNone(phone2mac.sniff(b"hello"))


if __name__ == "__main__":
    unittest.main()
