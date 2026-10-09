# Spray planning pipeline

Python 3.11 ya 3.12 use karo (3.14 par torch/ultralytics compatibility verify nahi hui).

```
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pytest -q
python run_e2e.py flight.mp4 flight.SRT boundary.json best.pt
streamlit run app_approval.py
```

Files:
- gis_pipeline.py: georeference, filter, cluster, grid, export, save/load
- test_gis_pipeline.py: edge case tests
- run_e2e.py: video + SRT + boundary -> plan.png + report_draft.json
- app_approval.py: Streamlit approval screen + exports

Kya verify nahi hua: real video, real SRT, asli drone pe spray. Pehle plan.png ko satellite map se milao.
Pass hone ke baad `pip freeze > requirements.lock` se versions pin kar lo.
