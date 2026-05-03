# YaDisk Chat

> P2P messenger with E2E encryption built on top of Yandex.Disk. One Python file. Zero dependencies. No server.






***

## How it works

Each participant stores their own chat history on their **Yandex.Disk**. Messages are written to `JSONL` files that the other party reads via a public link. No relay server — Yandex.Disk is used purely as a transport.

```
Alice                              Bob
  │  disk:/YaChatV2/contacts/       │  disk:/YaChatV2/contacts/
  │    └─ contact-XYZ/              │    └─ contact-ABC/
  │         inbox.jsonl  ◄──────────┤         inbox.jsonl  ◄──────
  │         attachments/ (pub)      │         attachments/ (pub)
  └──────────────────────────────────┘
              public links (read-only)
```

No message ever passes through a third-party server. The app runs locally and talks directly to the Yandex.Disk API.

***

## Features

- Text messages — instant send, auto-refresh (~5 s polling)
- E2E encryption — AES-256-GCM + PBKDF2-SHA-256 (120,000 iterations); encryption runs entirely in the browser, the local server never sees the key
- Images — send, inline preview, download via built-in proxy
- Any file type — send and download
- Multiple contacts — one-click switching, unread badge
- Dark / light theme — follows system preference, manual override
- Audio notification on new message (Web Audio API, no external files)
- Auto-restore contacts and history on restart from the disk folder structure

***

## Quick start

### 1. Create a Yandex OAuth application

1. Go to [oauth.yandex.ru](https://oauth.yandex.ru) → **Create application**
2. Type: **Web service**
3. Permissions: `cloud_api:disk.read` + `cloud_api:disk.write`
4. Callback URI: `http://127.0.0.1:8765/auth/callback`
5. Save your **ClientID**

### 2. Start the server

```bash
python server.py
```

The app opens in your browser at **http://127.0.0.1:8765**

### 3. Sign in

In the sidebar enter your **ClientID** and username → click **Sign in with Yandex**.  
Alternatively, paste a token manually using the "manual" button.

### 4. Set up the disk

Click **Set up disk** — the app creates the folder structure `disk:/YaChatV2/` on your Yandex.Disk.

### 5. Add a contact

1. Click **+ Add contact** — your public chat link appears
2. Share it with the other person (one time only)
3. Paste their link → **Add**

***

## Disk data structure

```
disk:/YaChatV2/
└─ contacts/
   └─ contact-{id}/
      ├─ inbox.jsonl        ← your outgoing messages (public, read-only)
      └─ attachments/
         └─ {rand}.{ext}    ← attachments (public)
```

Each line of `inbox.jsonl` is a single JSON object:

```jsonc
// Plain text message
{"id":"msg-abc123","kind":"text","from":"alice","ts":1700000000000,
 "payload":{"enc_stub":"plain","text":"Hello!"}}

// Encrypted message (AES-256-GCM)
{"id":"msg-def456","kind":"text","from":"alice","ts":1700000001000,
 "payload":{"v":1,"alg":"AES-GCM","iv":"<base64>","ct":"<base64>"}}

// File or image
{"id":"msg-ghi789","kind":"image","from":"alice","ts":1700000002000,
 "name":"photo.jpg","size":204800,"path":"disk:/YaChatV2/contacts/.../photo.jpg"}
```

***

## Encryption

Encryption is **optional** and configured per contact (lock button in the chat header).

| Parameter | Value |
|---|---|
| Algorithm | AES-256-GCM |
| Key derivation | PBKDF2-SHA-256, 120,000 iterations |
| Salt | `YaChatV2-e2e-v1` (fixed) |
| IV | 12 bytes, random per message |
| Where it runs | Browser only (Web Crypto API) |
| What the server sees | Encrypted blob — cannot decrypt |
| Key storage | Browser tab memory only, never persisted |

> Both parties must use the **same password**. Share it through a separate secure channel.

***

## Requirements

- Python **3.8+**
- Standard library only: `http.server`, `urllib`, `json`, `threading`
- A Yandex account with Yandex.Disk access

***

## Known limitations

- Not real-time: new messages arrive after ~5 s (polling)
- No OS push notifications (browser audio only)
- History lives with the sender; deleting the file destroys it
- OAuth token is in-memory only — re-authentication needed after restart

***

## Roadmap

| Version | Feature |
|---|---|
| v1.1 | Token persistence — save OAuth token between restarts |
| v1.2 | Interface improvements — drag & drop upload, message search, OS notifications, audio fix, adaptive polling (active chats every 5 s → idle contacts up to 1 h) |
| v1.3 | Alternative storage backends — Google Drive, Dropbox, S3 |
| v1.4 | Blog mode — public read-only channel (write without replies; subscribe by link, no auth required) |
| v2.0 | Group chats — multiple subscribers on a single inbox |

***

## License

[MIT](LICENSE) — Copyright (c) 2026 iwojima-dev
