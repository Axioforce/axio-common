#!/bin/bash

set -e  # Exit immediately on any error
source ~/anaconda3/etc/profile.d/conda.sh  # Load conda functions

cd ~/Documents/axio-common

echo "Activating axio-server..."
conda activate axio-server
echo "Updating axio-common in axio-server environment..."
pip install -e .
conda deactivate

echo "Activating axio-dash..."
conda activate axio-dash
echo "Updating axio-common in axio-dash environment..."
pip install -e .
conda deactivate

echo "✅ axio-common updated in both environments."
