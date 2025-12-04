name: Dynamic website monitor

on:
  schedule:
    - cron: "*/5 * * * *"
  repository_dispatch:
    types: [dynamic-monitor]
  workflow_dispatch:

concurrency:
  group: housing-monitor-all-v2  # 🟢 FIX: Unified concurrency group
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  check-dynamic-sites:
    runs-on: ubuntu-latest
    timeout-minutes: 15          # 🟢 FIX: Add workflow timeout

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          pip install requests beautifulsoup4 lxml playwright
          python -m playwright install --with-deps chromium

      - name: Run dynamic monitor script
        env:
          NTFY_TOPIC_URL: ${{ secrets.NTFY_TOPIC_URL }}
          DEBUG: "false"
        run: |
          python monitor_dynamic.py

      - name: Commit updated dynamic state
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"

          # Include all state files (*.json)
          if [[ -n "$(git status --porcelain '*.json')" ]]; then
            echo "Dynamic state files changed, committing..."
            
            # CRITICAL FIX: Add and Commit FIRST to avoid Git errors
            git add *.json
            git commit -m "Update dynamic monitor state [skip ci]" || echo "Nothing to commit"

            # Pull and Push with retry loop for resilience against race conditions
            for i in {1..3}; do
              git pull --rebase origin main
              if git push origin HEAD:main; then
                echo "Dynamic state committed successfully"
                exit 0
              fi
              echo "Push failed, retrying ($i/3)..."
              sleep 2
            done
            echo "ERROR: Failed to push state after 3 attempts"
            exit 1
          else
            echo "No changes to commit"
          fi
