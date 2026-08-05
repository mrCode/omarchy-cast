# Testing the Cast backend against real hardware

The Cast path has never talked to a Chromecast — it is covered by unit tests
against fakes only. Everything below is our own code: pychromecast, the Default
Media Receiver launch, the GStreamer pipeline and the chunked HTTP server.

Today's session is the argument for this checklist: three real bugs in the
AirPlay path were invisible to tests and only showed up against hardware, and
one of them was a green "streaming" status for a stream with no pixels in it.
**Do not trust a success message. Check what actually happened.**

## Before you start

```bash
sudo ufw allow proto tcp from <your-subnet> to any port 8010
```

The Chromecast fetches the video *from* this machine. Without this rule a
default-DROP firewall discards the fetch and the failure looks like the
receiver rejecting the stream.

## Steps

1. **Discovery.** `omarchy-cast list` should show the Chromecast. If it does
   not, the AP may not be forwarding multicast — use
   `omarchy-cast start --address <ip> --protocol cast`.

2. **Start.** `omarchy-cast start cast:<id>`. Expect a portal prompt on first
   run only.

3. **Confirm pixels, not just status.** `omarchy-cast status` saying
   `streaming` is not proof. Check the TV, and check the server actually served
   bytes:

   ```bash
   ss -tnp | grep 8010
   ```

   A connected Chromecast should hold an ESTABLISHED connection.

4. **Measure the latency.** Put a clock on screen and photograph both. The
   README claims 1–3 s for Cast; if it is far off, the claim needs correcting.

5. **Stop.** `omarchy-cast stop`, then confirm nothing is left behind:

   ```bash
   pgrep -af 'gst-launch|omarchy_cast' ; ss -tnp | grep 8010
   ```

6. **Kill it mid-stream** (`pkill -f gst-launch`) and confirm the session goes
   FAILED with a notification rather than sitting green forever.

## Things most likely to be wrong

- **The receiver cannot reach `8010`** — firewall, or bound to the wrong
  interface. `local_address_for()` picks the interface facing the receiver;
  verify with `ss -tlnp | grep 8010` that it is not on a docker bridge.
- **The receiver rejects `video/x-matroska`** — the Default Media Receiver is
  fussy about containers. If LOAD fails, try fragmented MP4 instead of
  Matroska.
- **The stream starts and stalls** — likely the receiver buffering and never
  getting a keyframe. `key-int-max` is set from `fps`, so a keyframe should
  arrive every second; check the encoder actually honoured it.
- **`pychromecast` connects but the app never reaches PLAYING** — check the app
  ID is still `CC1AD845` and that another sender has not taken the device.

## Record the result

Update `docs/device-matrix.md` either way. A confirmed failure is worth as much
as a confirmed success, and the matrix currently claims nothing about Cast.
