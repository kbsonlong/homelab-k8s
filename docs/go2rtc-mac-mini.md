# go2rtc on Mac mini

## Scope

Run go2rtc natively on the Apple Silicon Mac mini to ingest Xiaomi cameras through the Xiaomi Mi Home source. go2rtc handles camera connectivity and stream restreaming; it does not perform face recognition.

## Current lab deployment

- Host: macOS 26.6.2, Apple Silicon, LAN address `192.168.3.18` on `en1`.
- Binary: official go2rtc `v1.9.14`, `darwin/arm64`.
- Binary path: `/Volumes/nfs/services/go2rtc/bin/go2rtc`.
- Config: `/Volumes/nfs/services/go2rtc/config/go2rtc.yaml` (mode 600; contains the Web UI password and must remain untracked).
- Logs: `/Volumes/nfs/services/go2rtc/log/`.
- Web UI: `http://192.168.3.18:1984/`.
- RTSP output: `rtsp://192.168.3.18:8554/<stream>`.
- WebRTC listener: `192.168.3.18:8555`.
- All listeners bind to the Mac LAN address; no public ingress or router port forwarding.
- go2rtc UI is protected with a generated local username/password. Retrieve it from the local config file; never copy it into GitHub.
- Xiaomi account credentials and tokens are added in the go2rtc Web UI and must not be committed.

## Xiaomi camera support

`chuangmi.camera.069a01` (Xiaomi Smart Camera 3 PTZ Version) is listed in the go2rtc known Xiaomi camera list, using `cs2+tcp`, HEVC video and Opus audio. On this Mac mini with go2rtc v1.9.14, its RTSP restream was probed successfully and reported HEVC 2960x1666 video plus Opus audio.

`xiaovv.camera.fjcam2` (Xiaovv Smart Dual Camera S1 PTZ) is not listed as a verified model in the go2rtc known-camera list. It is configured as a second source, but its RTSP restream returned `404 Not Found` during verification. A later probe found the Mac's ARP entry for `192.168.3.165` incomplete, with ping and TCP ports 80/443/554/8000/8080 all timing out or reporting host down. This means the camera was not reachable on the LAN during the probe, so protocol support cannot yet be isolated. First confirm the camera is powered, online in Xiaomi Home, still using `192.168.3.165`, and on the same LAN/VLAN as the Mac; then retry. Once reachable, test its Xiaomi producer again before trying generic RTSP paths. A public go2rtc issue reports CS2 timeouts on some Apple Silicon systems with v1.9.14. The `069a01` stream worked in this installation, but this issue may be relevant when troubleshooting other CS2 cameras.

The local stream names are `xiaomi_camera3_ptz` and `xiaovv_s1`. Add the account from **Web UI → Add → Xiaomi** if authentication has not yet been saved. Select the account region and complete any Xiaomi verification challenge in the browser. Xiaomi's encryption key retrieval requires internet access on each camera connection; media is transported locally after the connection is established. Keep the go2rtc config and account token private.

## Run and verify

The supervised execution environment blocks registering a LaunchAgent. The service was therefore started as a managed background process for the current host session. After a host reboot or process exit, start it again with:

```bash
/Volumes/nfs/services/go2rtc/bin/go2rtc -c /Volumes/nfs/services/go2rtc/config/go2rtc.yaml
```

Verify listeners and UI (replace credentials by reading the local config; do not paste secrets in shared logs):

```bash
lsof -nP -iTCP:1984 -sTCP:LISTEN
lsof -nP -iTCP:8554 -sTCP:LISTEN
curl -u admin:<local-password> -o /dev/null -w '%{http_code}\n' http://192.168.3.18:1984/
curl -o /dev/null -w '%{http_code}\n' http://192.168.3.18:1984/
```

Expected: authorized UI request returns `200`, unauthenticated request returns `401`, and listeners bind only to `192.168.3.18`.

After adding the Xiaomi account, check **Streams** in the UI and confirm each camera has video packets and a live preview. The stream API is available at `/api/streams` (requires UI authentication). The known working stream can also be probed with:

```bash
ffprobe -v error -rtsp_transport tcp -show_entries stream=codec_name,codec_type,width,height -of json rtsp://192.168.3.18:8554/xiaomi_camera3_ptz
```

Do not declare a camera integration complete until the preview and stream statistics are verified. In this deployment, `xiaovv_s1` currently returns RTSP 404 and is not yet verified.

## Recovery and credential hygiene

- Keep `/Volumes/nfs/services/go2rtc/config/go2rtc.yaml` private and backed up securely; it contains the UI password and may contain Xiaomi account tokens after login.
- Do not commit the real config, Xiaomi credentials, verification codes, tokens, or captured face/video data.
- If Web UI authentication is lost, stop go2rtc, edit the local config, restart it, then retest authenticated and unauthenticated requests.
- If the camera fails to reconnect, confirm the Mac has internet access for key retrieval, the Xiaomi account region is correct, and inspect go2rtc logs before changing camera settings.

## References

- go2rtc Xiaomi source: <https://go2rtc.org/internal/xiaomi>
- go2rtc known Xiaomi camera list: <https://github.com/AlexxIT/go2rtc/issues/1982>
- go2rtc v1.9.14 release: <https://github.com/AlexxIT/go2rtc/releases/tag/v1.9.14>
