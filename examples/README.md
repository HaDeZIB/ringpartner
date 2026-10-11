# ringpartner examples

Three short, runnable scripts. Run them from this folder after `pip install ringpartner` (or
`uv sync --all-packages` at the DoorLedger repo root).

| Script | What it shows | Needs |
|---|---|---|
| [`list_devices_history.py`](list_devices_history.py) | List devices (online, E2EE, chime) and recent history filtered with `event_types`, with pagination | `RING_ACCESS_TOKEN`, or `--replay <fixtures dir>` offline |
| [`whep_to_opencv.py`](whep_to_opencv.py) | Open a server-side WHEP live view, get NumPy frames, run an OpenCV motion gate, save changed frames | `pip install 'ringpartner[whep]' opencv-python-headless` and `RING_ACCESS_TOKEN` |
| [`webhook_receiver.py`](webhook_receiver.py) | Verify `X-Signature` over the raw body with a stdlib `http.server`, dedupe on `request_id`, answer fast. `--self-test` uses the signed simulator to send a valid, a duplicate and a tampered webhook | Nothing (self-test), or `RING_HMAC_KEY` |

```bash
python webhook_receiver.py --self-test                                  # offline, prints PASS x4
python list_devices_history.py --replay ../../../fixtures/playground    # offline, recorded Playground responses
RING_ACCESS_TOKEN=... python list_devices_history.py --types motion.human,ding
RING_ACCESS_TOKEN=... python whep_to_opencv.py --seconds 20 --fps 2 --out frames/
```

A Ring Developer Playground token lasts about 30 minutes. The Playground streams a simulated
live view but sends no webhooks, which is why the webhook example uses the simulator.
