from pathlib import Path

path = Path('.github/workflows/ci.yml')
text = path.read_text(encoding='utf-8')
old_checkout = '''      - name: Checkout exact head with bounded history
        uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4
        timeout-minutes: 10
        with:
          fetch-depth: 100
          ref: ${{ github.event.pull_request.head.sha || github.sha }}

      - name: Verify exact head
'''
new_checkout = '''      - name: Checkout exact head
        uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4
        timeout-minutes: 10
        with:
          fetch-depth: 1
          ref: ${{ github.event.pull_request.head.sha || github.sha }}

      - name: Verify exact head
'''
if text.count(old_checkout) != 1:
    raise SystemExit('ci.yml: bounded checkout block drifted')
text = text.replace(old_checkout, new_checkout)
verify_block = '''      - name: Verify exact head
        shell: bash
        run: |
          set -euo pipefail
          test "$(git rev-parse HEAD)" = "${{ github.event.pull_request.head.sha || github.sha }}"

      - name: Install uv
'''
fetch_block = '''      - name: Verify exact head
        shell: bash
        run: |
          set -euo pipefail
          test "$(git rev-parse HEAD)" = "${{ github.event.pull_request.head.sha || github.sha }}"

      - name: Fetch bounded PR history without historical blobs
        if: ${{ github.event_name == 'pull_request' }}
        shell: bash
        env:
          PR_HEAD_REF: ${{ github.event.pull_request.head.ref }}
          PR_BASE_SHA: ${{ github.event.pull_request.base.sha }}
        run: |
          set -euo pipefail
          git config remote.origin.promisor true
          git config remote.origin.partialclonefilter blob:none
          git fetch --no-tags --filter=blob:none --deepen=100 origin "$PR_HEAD_REF"
          if ! git cat-file -e "${PR_BASE_SHA}^{commit}" 2>/dev/null; then
            git fetch --no-tags --filter=blob:none --depth=1 origin "$PR_BASE_SHA"
          fi

      - name: Install uv
'''
# Verify block occurs in all three jobs; replace only the final Security occurrence.
index = text.rfind(verify_block)
if index < 0:
    raise SystemExit('ci.yml: Security verify block not found')
text = text[:index] + text[index:].replace(verify_block, fetch_block, 1)
path.write_text(text, encoding='utf-8')
