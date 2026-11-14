# Edge Impulse model

Put the Edge Impulse **Linux (AARCH64)** export here. `EI_MODEL_PATH` in
`python/config.py` names `noise-classifier.eim`; if that file doesn't exist, the
newest `.eim` in this folder is used. Without any model, loudness is still
measured and uploaded, but `sources` is empty and readings are flagged `no_model`.

1. Export the model from Edge Impulse (Deployment -> Linux (AARCH64)), or on the board:
   `edge-impulse-linux-runner --download models/noise-classifier.eim`
2. Nothing to install: `python/classifier.py` talks to the `.eim` over its local socket
   using only the Python standard library.
3. Check that it loads: `python3 python/classifier.py`. This prints the model's labels, window size and sample rate.
4. If the model's labels differ from the names you want on the map, map them in
   `EI_LABEL_MAP` (`"car": "traffic"`, `"silence": ""` to drop a class).

The mic is opened at the model's own sample rate, and each full model window
(`input_features_count` raw samples, usually 1 s) is classified. Scores are averaged
over each reading. The model's DSP block runs inside the `.eim`, so retraining
needs no code changes: drop in the new file and restart.
