# Proposed Refactor Layout

The prototype should move toward the following package structure:

```text
asmgcs/
  __init__.py
  app/
    bootstrap.py
    composition.py
  domain/
    __init__.py
    contracts.py
    enums.py
  fusion/
    __init__.py
    tracking.py
    telemetry.py
  physics/
    __init__.py
    engine.py
    geometry.py
  routing/
    __init__.py
    predictor.py
  viewmodels/
    __init__.py
    radar_viewmodel.py
    telemetry_viewmodel.py
  views/
    __init__.py
    radar_scene.py
    graphics_items.py
    main_window.py
  infrastructure/
    __init__.py
    opensky_client.py
    overpass_repository.py
    thread_hosts.py
tests/
  test_tracking.py
  test_physics_engine.py
```

This initial refactor phase implements the domain contracts, the smoothing/data-fusion model, and the multithreaded physics/collision engine as standalone modules that can be integrated by a future radar viewmodel.