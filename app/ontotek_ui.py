"""Local test UI for the OntoTek NL agent (ontotek_agent.py -> server.py).

Lets you type a natural-language question, see the intent JSON the local LLM
(qwen2.5-coder:3b via Ollama) generated from it, and the Cube result it produced —
plus a manual/advanced mode for hand-crafting intents directly against
compile_and_execute_intent, bypassing the LLM.

Run: python3 test_ui.py
Then open http://localhost:8765
"""
import json
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

import server as ontotek_server
import ontotek_agent

PORT = 8765


def render_page():
    ontology = ontotek_server.ONTOLOGY_CACHE
    metrics = ontology.get("metrics", {})
    dimensions = ontology.get("dimensions", {})

    domains = sorted({
        meta.get("domain")
        for meta in list(metrics.values()) + list(dimensions.values())
        if meta.get("domain")
    })
    domain_options = '<option value="">Auto-detect</option>' + "".join(
        f'<option value="{d}">{d}</option>' for d in domains
    )

    metric_checks = "\n".join(
        f'<label class="row"><input type="checkbox" name="metric" value="{mid}"> '
        f'<code>{mid}</code> <span class="dim">&rarr; {m.get("cube_measure", "")}</span></label>'
        for mid, m in metrics.items()
    )
    dim_checks = "\n".join(
        f'<label class="row"><input type="checkbox" name="dimension" value="{did}"> '
        f'<code>{did}</code> <span class="dim">&rarr; {d.get("cube_dimension", "")}</span></label>'
        for did, d in dimensions.items()
    )

    filter_options = "\n".join(f'<option value="{did}">{did}</option>' for did in dimensions)

    allowed_values_js = json.dumps({did: d.get("allowed_values", []) for did, d in dimensions.items()})

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>OntoTek Intent Tester</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
  h1 {{ font-size: 1.3rem; }}
  fieldset {{ border: 1px solid #ddd; border-radius: 6px; margin-bottom: 1rem; }}
  legend {{ font-weight: 600; padding: 0 0.4rem; }}
  .row {{ display: block; margin: 0.3rem 0; }}
  .dim {{ color: #888; font-size: 0.85em; }}
  code {{ background: #f4f4f4; padding: 1px 4px; border-radius: 3px; }}
  #filters .filter-row {{ display: flex; gap: 0.5rem; margin: 0.3rem 0; align-items: center; }}
  select, input[type=text], textarea {{ font-family: inherit; padding: 4px; }}
  button {{ padding: 6px 14px; border-radius: 5px; border: 1px solid #888; background: #f0f0f0; cursor: pointer; }}
  button:hover {{ background: #e4e4e4; }}
  textarea {{ width: 100%; box-sizing: border-box; height: 6rem; font-family: monospace; }}
  pre#result, .step pre {{ background: #111; color: #b7f5b7; padding: 1rem; border-radius: 6px; overflow-x: auto; white-space: pre-wrap; }}
  .remove {{ cursor: pointer; color: #b33; }}
  #question {{ width: 100%; box-sizing: border-box; padding: 8px; font-size: 1rem; }}
  details {{ margin-top: 1.5rem; }}
  summary {{ cursor: pointer; font-weight: 600; color: #555; }}
  .step {{ margin: 0.6rem 0; }}
  .step h4 {{ margin: 0 0 0.2rem 0; font-size: 0.85rem; color: #666; text-transform: uppercase; letter-spacing: 0.03em; }}
</style>
</head>
<body>
<h1>OntoTek Agent( Beta )</h1>
<p style="color:#666">Ask a business question. Autonomous Semantic Query Agent reads the
OntoTek ontology (synced from OpenMetadata) and translates it into an intent JSON, which is then
compiled to a Cube query and executed.</p>

<fieldset>
  <legend>Ask the agent</legend>
  <input id="question" type="text" placeholder="e.g. What was the box office for Sci-Fi movies?"
         onkeydown="if(event.key==='Enter') askAgent();">
  <div style="margin-top:0.6rem;">
    <label for="domain-select" style="margin-right:0.4rem; color:#555;">Ontology domain:</label>
    <select id="domain-select">{domain_options}</select>
  </div>
  <div style="margin-top:0.6rem;">
    <button onclick="askAgent()">Ask</button>
    <button onclick="showSchema()" style="margin-left:0.5rem;">View raw ontology</button>
  </div>
</fieldset>

<div id="agent-result"></div>

<details>
<summary>Manual / advanced mode (build an intent by hand, bypassing the LLM)</summary>

<fieldset>
  <legend>Metrics</legend>
  {metric_checks or "<em>none defined</em>"}
</fieldset>

<fieldset>
  <legend>Dimensions</legend>
  {dim_checks or "<em>none defined</em>"}
</fieldset>

<fieldset>
  <legend>Filters</legend>
  <div id="filters"></div>
  <button type="button" onclick="addFilter()">+ Add filter</button>
</fieldset>

<fieldset>
  <legend>Intent JSON (built automatically — edit freely, e.g. to test invalid values)</legend>
  <textarea id="intent"></textarea>
</fieldset>

<button onclick="runIntent()">Run</button>

<h3>Result</h3>
<pre id="result">(nothing run yet)</pre>
</details>

<script>
const ALLOWED_VALUES = {allowed_values_js};
const FILTER_DIMS = document.querySelectorAll('#filters');

function addFilter() {{
  const container = document.getElementById('filters');
  const row = document.createElement('div');
  row.className = 'filter-row';
  const dimOptions = `{filter_options}`;
  row.innerHTML = `
    <select class="f-dim" onchange="onDimChange(this)">
      <option value="">-- dimension --</option>
      ${{dimOptions}}
    </select>
    <select class="f-val"><option value="">-- value --</option></select>
    <span class="remove" onclick="this.parentElement.remove(); syncIntent();">remove</span>
  `;
  container.appendChild(row);
  row.querySelector('.f-dim').addEventListener('change', syncIntent);
}}

function onDimChange(sel) {{
  const row = sel.closest('.filter-row');
  const valSel = row.querySelector('.f-val');
  const values = ALLOWED_VALUES[sel.value] || [];
  valSel.innerHTML = '<option value="">-- value --</option>' +
    values.map(v => `<option value="${{v}}">${{v}}</option>`).join('') +
    '<option value="__custom__">(type custom / invalid value)</option>';
  valSel.onchange = () => {{
    if (valSel.value === '__custom__') {{
      const custom = prompt('Enter a custom value to test (e.g. an invalid one):', '');
      if (custom !== null) {{
        const opt = document.createElement('option');
        opt.value = custom; opt.textContent = custom + ' (custom)';
        valSel.appendChild(opt);
        valSel.value = custom;
      }}
    }}
    syncIntent();
  }};
}}

function syncIntent() {{
  const metrics = [...document.querySelectorAll('input[name=metric]:checked')].map(i => i.value);
  const dimensions = [...document.querySelectorAll('input[name=dimension]:checked')].map(i => i.value);
  const filters = [...document.querySelectorAll('.filter-row')].map(row => {{
    const dim = row.querySelector('.f-dim').value;
    const val = row.querySelector('.f-val').value;
    return dim && val ? {{dimension: dim, value: val}} : null;
  }}).filter(Boolean);
  document.getElementById('intent').value = JSON.stringify({{metrics, dimensions, filters}}, null, 2);
}}

document.querySelectorAll('input[name=metric], input[name=dimension]').forEach(el => {{
  el.addEventListener('change', syncIntent);
}});

async function runIntent() {{
  const resultEl = document.getElementById('result');
  resultEl.textContent = 'Running...';
  try {{
    const intent = document.getElementById('intent').value;
    JSON.parse(intent); // validate client-side first
    const resp = await fetch('/run', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{intent_json: intent}})
    }});
    const text = await resp.text();
    resultEl.textContent = text;
  }} catch (e) {{
    resultEl.textContent = 'Client-side error: ' + e.message;
  }}
}}

async function showSchema() {{
  const resultEl = document.getElementById('result');
  resultEl.textContent = 'Loading...';
  const resp = await fetch('/schema');
  resultEl.textContent = await resp.text();
}}

function escapeHtml(str) {{
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}}

async function askAgent() {{
  const question = document.getElementById('question').value.trim();
  const domain = document.getElementById('domain-select').value;
  const out = document.getElementById('agent-result');
  if (!question) return;
  out.innerHTML = '<p style="color:#888">Thinking...</p>';
  try {{
    const resp = await fetch('/ask', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{question, domain}})
    }});
    const body = await resp.json();
    if (body.error) {{
      out.innerHTML = `<div class="step"><h4>Error</h4><pre>${{body.error}}</pre></div>`;
      return;
    }}
    const answerHtml = body.chat_response
      ? `<div class="step"><h4>Answer</h4><pre>${{escapeHtml(body.chat_response)}}</pre></div>`
      : '';
    out.innerHTML = `
      ${{answerHtml}}
      <div class="step"><h4>Generated intent</h4><pre>${{JSON.stringify(body.generated_intent, null, 2)}}</pre></div>
      <div class="step"><h4>Execution result</h4><pre>${{JSON.stringify(body.result, null, 2)}}</pre></div>
    `;
  }} catch (e) {{
    out.innerHTML = `<div class="step"><h4>Client-side error</h4><pre>${{e.message}}</pre></div>`;
  }}
}}

addFilter();
syncIntent();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, content_type="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(200, render_page())
        elif self.path == "/schema":
            self._send(200, json.dumps(ontotek_server.ONTOLOGY_CACHE, indent=2), "application/json")
        else:
            self._send(404, "Not found")

    def do_POST(self):
        if self.path == "/run":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            result = ontotek_server.compile_and_execute_intent(body.get("intent_json", "{}"))
            self._send(200, result, "application/json")
        elif self.path == "/ask":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            question = body.get("question", "")
            domain = body.get("domain") or None
            try:
                intent = ontotek_agent.build_intent_from_question(question, domain=domain)
                intent_dict = intent.model_dump()
                raw_result = ontotek_server.compile_and_execute_intent(json.dumps(intent_dict))
                try:
                    result = json.loads(raw_result)
                except json.JSONDecodeError:
                    result = raw_result  # e.g. the plain-string "OntoTek Error: ..." rejection

                if isinstance(result, dict):
                    try:
                        chat_response = ontotek_agent.synthesize_answer(question, result)
                    except Exception:
                        # Raw result/intent are still shown even if the synthesis
                        # call itself fails (Ollama down, etc.) — degrade gracefully
                        # rather than losing an otherwise-successful query.
                        chat_response = None
                else:
                    chat_response = result  # already a plain-English rejection message

                self._send(200, json.dumps({
                    "generated_intent": intent_dict,
                    "result": result,
                    "chat_response": chat_response,
                }), "application/json")
            except Exception:
                self._send(200, json.dumps({"error": traceback.format_exc()}), "application/json")
        else:
            self._send(404, "Not found")

    def log_message(self, fmt, *args):
        pass  # keep stdout quiet


if __name__ == "__main__":
    # "0.0.0.0", not "localhost" -- inside a container, binding to localhost
    # only accepts connections from within the container's own network
    # namespace, so a published port (docker-compose.yml's app service)
    # would have nothing listening on the external interface to forward to.
    httpd = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"OntoTek test UI running at http://localhost:{PORT}")
    httpd.serve_forever()
