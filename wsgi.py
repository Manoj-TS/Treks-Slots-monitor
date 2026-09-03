"""WSGI entry point. Run with: waitress-serve ... wsgi:app  (or `python wsgi.py` for dev)."""

from aranya import create_app

app = create_app()

if __name__ == "__main__":
    import os

    from aranya.config import HOST, PORT

    print("=" * 52)
    print("  ARANYA - TREK SLOT BOARD")
    print("  Weekends / Calendar / Favourites / Settings")
    print("  Unofficial. Availability display only.")
    print(f"  Open: http://localhost:{PORT}")
    print("=" * 52)
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
