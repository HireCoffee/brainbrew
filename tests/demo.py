"""Synthetic material for rendering checks; never represents the real curriculum."""
import argparse
from pathlib import Path
import json
import yaml


def create(root):
    root = Path(root)
    session = root / "content/student/introduction-au-mlops/session-01"
    (session / "resources").mkdir(parents=True)
    data = {"courses": [{"id": "introduction-au-mlops", "title": "Introduction au MLOps",
        "description": "Démonstration technique fictive du moteur de cours.", "timezone": "Europe/Paris",
        "sessions": [{"id": "session-01", "title": "Séance de démonstration",
                      "starts_at": "2020-01-01T10:00:00+01:00", "ready": True},
                     {"id": "session-02", "title": "Une prochaine séance",
                      "starts_at": "2099-01-01T10:00:00+01:00", "ready": True}]}]}
    (root / "catalog.yml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    (session / "article.qmd").write_text(r'''## Comprendre avant de déployer

Cette page sert uniquement à vérifier le moteur de publication.

Une formule en ligne : $f(x) = wx + b$. Une équation affichée :

$$
\operatorname{WER} = \frac{S + D + I}{N}
$$

## Expérimenter

Le notebook conserve le code rempli et commence sans résultats calculés.

```python
def predict(x, w=2, b=1):
    return w * x + b
```

::: {.callout-note}
Le rendu du cours n'exécute aucun code Python.
:::
''', encoding="utf-8")
    (session / "resources.yml").write_text("resources:\n  - path: exemple.ipynb\n    label: Notebook de démonstration\n  - path: predict.py\n    label: Script Python\n", encoding="utf-8")
    nb = {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": [
        {"cell_type": "code", "id": "demo", "metadata": {"secret": "PRIVATE_OUTPUT_SENTINEL"},
         "source": ["print(2 * 3 + 1)"], "execution_count": 1,
         "outputs": [{"output_type": "stream", "name": "stdout", "text": ["PRIVATE_OUTPUT_SENTINEL"]}]}]}
    (session / "resources/exemple.ipynb").write_text(json.dumps(nb), encoding="utf-8")
    (session / "resources/predict.py").write_text("def predict(x, w=2, b=1):\n    return w * x + b\n", encoding="utf-8")
    future = root / "content/student/introduction-au-mlops/session-02"
    future.mkdir(parents=True)
    (future / "article.qmd").write_text("FUTURE_CONTENT_SENTINEL", encoding="utf-8")
    return root / "catalog.yml", root / "content"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    create(parser.parse_args().output)
