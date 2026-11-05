# Phone2Mac

Reliable two-way copy & paste between your phone and your Mac: **text, links, photos, videos, PDFs, any file**.

Universal Clipboard depends on iCloud, Bluetooth and Handoff, and it often breaks without telling you.
Phone2Mac doesn't use any of those. A small server runs on your Mac and your phone talks to it directly over
your Wi-Fi, so it works the same way every time. It also works with Android phones.

| You do this | What happens |
|---|---|
| Copy anything on the Mac (⌘C): text, a link, a screenshot, files in Finder | It appears on your phone instantly, with one tap to copy or save |
| Paste or share anything on the phone | Text is put straight on the Mac clipboard (just press ⌘V). Files are saved to `~/Phone2Mac` **and** put on the clipboard, so ⌘V pastes them into Finder, Mail, Messages, Slack and other apps |

## Install (Mac, about 1 minute)

```bash
git clone https://github.com/afonjatee2/phone2mac.git
```
```bash
cd phone2mac && ./install.sh
```

The installer:
- runs Phone2Mac in the background, **starts it automatically at login**, and **restarts it if it ever crashes**
- opens `http://localhost:8765/pair` with a QR code

It doesn't install anything else. It uses the Python that comes with Apple's Command Line Tools. If the installer says
Python is missing, run `xcode-select --install` first.

If macOS asks *“Allow Python to accept incoming network connections?”*, click **Allow**.

## Pair your phone

1. Scan the QR code on the pairing page with your phone's camera. Phone and Mac must be on the same Wi-Fi.
2. In Safari/Chrome: **Share → Add to Home Screen**. You now have a Phone2Mac “app”.

On the phone:
- **Send to the Mac:** tap the box, tap **Paste**. It's sent immediately. Or tap **Photos & files** to send anything.
- **Get from the Mac:** copy on the Mac, then tap **Copy** on the phone. For photos and files, use **Save** or **Open**.

### iPhone: one-tap Shortcuts and Back Tap (recommended)

Web pages on iOS can't touch your clipboard in the background, so two small Shortcuts make it truly one-tap.
**You don't build them yourself.** Phone2Mac generates them with your Mac's address and key already filled in,
and signs them on your Mac so the iPhone accepts them. Use either option:

- **On the iPhone:** open the Phone2Mac page and scroll to the bottom. Tap **Add “Send to Mac”** and **Add “Get from Mac”**, then **Add Shortcut**.
  (If Safari downloads the file instead, tap the ⬇︎ in the address bar and open it.)
- **On the Mac:** on the pairing page, click the two **Add** buttons, then **Add Shortcut** in the Shortcuts app.
  They sync to your iPhone through iCloud (Settings → [your name] → iCloud → Shortcuts must be on).

Then set up Back Tap: **Settings → Accessibility → Touch → Back Tap** (at the very bottom).
- **Double Tap** → “Get from Mac”: copies whatever is on the Mac clipboard to the phone.
- **Triple Tap** → “Send to Mac”: sends your phone's clipboard to the Mac, so you press ⌘V there.

Optionally add **“Share to Mac”** too. It appears in the Share Sheet of every app (Photos, Files, Safari, WhatsApp, ...) and can send several photos at once.
The first run asks to find devices on your **local network**. Tap **Allow**.

Generating the Shortcuts needs macOS 12+ and an iCloud sign-in on the Mac (Apple's `shortcuts sign` uses it).
If that's not possible, the pairing page has hand-building steps under *Build them by hand instead*.

### Android

The web page does everything. Add it to your home screen from Chrome's ⋮ menu. For a one-tap Share target, the free
**HTTP Shortcuts** app can POST to the same `/api/send` URL.

## Why it's reliable

- **No iCloud, Bluetooth or Handoff.** It's a plain connection over your own network.
- **Uses `YOUR-MAC.local`**, so it keeps working when your router gives the Mac a new IP address. The IP address is shown as a fallback.
- **Live updates that reconnect.** The page reconnects after the phone sleeps, Wi-Fi drops or the Mac restarts, and a status dot shows whether you're connected.
- **Self-healing service.** launchd restarts the server if it dies. The clipboard watcher has a watchdog, and it recycles itself regularly so memory stays flat.
- **Large files work.** Uploads stream to disk, failed uploads retry automatically, and videos support seeking.
- **History survives restarts.** The last 100 items are kept.
- **Never loops.** Things Phone2Mac puts on your clipboard are tagged, so they don't bounce back. Repeats are de-duplicated.
- **Respects password managers.** Clipboard items that 1Password and other password managers mark as concealed or transient are never captured.

## Security

- Everything stays on your network. Nothing goes to the cloud.
- Every request needs your pairing key, which comes from the QR code. The pairing page only opens on the Mac itself.
- To unpair every device, run `python3 ~/.phone2mac/app/phone2mac.py --reset-token`, then `./install.sh` again.
- Traffic is plain HTTP on your LAN, the same as most local tools. Avoid using it on untrusted public Wi-Fi.

## Away from home?

Install [Tailscale](https://tailscale.com) on the Mac and the phone, then use the Mac's Tailscale name or IP in place of `.local`.
Everything else stays the same.

## Files & folders

| Path | What |
|---|---|
| `~/Phone2Mac/` | Files received from your phone |
| `~/.phone2mac/` | History, pairing key, logs (`logs/phone2mac.log`) |
| `~/.phone2mac/app/` | The installed app |

**Uninstall:** `./uninstall.sh`. Your received files are kept.

## Troubleshooting

- **Phone says “Can't reach Mac”:** make sure both devices are on the same Wi-Fi and the Mac is awake. Try the IP-address link on the pairing page.
  Some guest or office networks block devices from talking to each other.
- **Nothing happens on the Mac:** run `tail -f ~/.phone2mac/logs/phone2mac.log`.
- **macOS asked for access to Desktop/Documents:** that happens when you copy a file from there and fetch it on the phone. Click Allow.
- **Run it in the foreground for debugging:** `./uninstall.sh`, then `python3 phone2mac.py -v`.

## Development

Pure Python standard library plus one HTML file, with no dependencies.

```bash
python3 -m unittest discover -s tests -v
```
```bash
python3 phone2mac.py --memory-clipboard
```

On non-Mac machines it uses an in-memory clipboard, so the server and UI can be developed and tested anywhere.
The clipboard code that talks to macOS is in `MacClipboard` (it uses `osascript` JXA, which ships with macOS).
