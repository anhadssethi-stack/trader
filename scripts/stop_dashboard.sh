#!/bin/bash
# Stops the dashboard server that NSE Research.app started.
pkill -f "streamlit run app/streamlit_app.py" && echo "Stopped." || echo "Not running."
