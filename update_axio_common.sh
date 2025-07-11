
set -e
source ~/anaconda3/etc/profile.d/conda.sh  # Or path to your conda install

cd ~/Documents/axio-common

conda activate axio-common

# Ask for minor or major version bump
read -r -p "What type of version bump? ([M]ajor/[m]inor/[p]atch/[s]kip) [p]: " bump_type
bump_type=${bump_type:-p}
if [[ $bump_type == "M" ]]; then
  bump-my-version bump major
elif [[ $bump_type == "m" ]]; then
  bump-my-version bump minor
elif [[ $bump_type == "p" ]]; then
  bump-my-version bump patch
elif [[ $bump_type == "s" ]]; then
  echo "Skipping version bump."
else
  echo "Invalid version bump type. Exiting."
  exit 1
fi

conda deactivate

# Show new version
NEW_VERSION=$(grep '^version =' pyproject.toml | cut -d '"' -f2)
echo "📦 Bumped axio-common to version $NEW_VERSION"

echo "📁 Installing in axio-server..."
conda activate axio-server
echo "Uninstalling existing axio-common..."
pip uninstall -y axio-common || true
echo "Reinstalling latest axio-common from GitHub..."
pip install --no-cache-dir -e git+https://github.com/Axioforce/axio-common.git#egg=axio_common
#echo "Checking axio-common path after update:"
#python3 -c "import axio_common, os; print(os.path.dirname(axio_common.__file__))"
#echo "Freezing requirements to requirements.txt..."
#pip freeze > requirements.txt
#git add requirements.txt
#git commit -m "Update requirements.txt for axio-common $NEW_VERSION"
#git push origin main
conda deactivate

echo "📁 Installing in axio-dash..."
conda activate axio-dash
echo "Uninstalling existing axio-common..."
pip uninstall -y axio-common || true
echo "Reinstalling latest axio-common from GitHub..."
pip install --no-cache-dir -e git+https://github.com/Axioforce/axio-common.git#egg=axio_common
#echo "Checking axio-common path after update:"
#python3 -c "import axio_common, os; print(os.path.dirname(axio_common.__file__))"
#echo "Freezing requirements to requirements.txt..."
#pip freeze > requirements.txt
#git add requirements.txt
#git commit -m "Update requirements.txt for axio-common $NEW_VERSION"
#git push origin main
conda deactivate

echo "✅ axio-common $NEW_VERSION installed in both environments."