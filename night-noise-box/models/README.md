# Edge Impulse model

Put the exported Edge Impulse **Linux (AARCH64)** model here as `noise-classifier.eim`
(path set by `EI_MODEL_PATH` in `python/config.py`). Until that file exists,
`python/analysis.py` runs with a placeholder classifier: loudness (LAeq / Lmax / L90)
is still measured and uploaded, `sources` is empty and readings carry the flag `no_model`.

1. Export the model from Edge Impulse (Deployment -> Linux (AARCH64)), or on the board:
   `edge-impulse-linux-runner --download models/noise-classifier.eim`
2. Install the Python SDK on the board: `pip3 install edge_impulse_linux`
3. Check that it loads: `python3 python/classifier.py`. This prints the model's labels, window size and sample rate.
4. If the model's labels differ from the names you want on the map, map them in
   `EI_LABEL_MAP` (`"car": "traffic"`, `"silence": ""` to drop a class).

The mic is opened at the model's own sample rate, and each full model window
(`input_features_count` raw samples, usually 1 s) is classified. Scores are averaged
over each reading. The model's DSP block runs inside the `.eim`, so retraining
needs no code changes: drop in the new file and restart.
