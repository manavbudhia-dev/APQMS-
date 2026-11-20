"""
Photo -> quotation: read a photographed quotation sheet (printed template
with handwritten edits and strikethroughs) with a vision-capable Claude
model, validate the structured result, let a person confirm it, then make
a normal quotation (and its PDF) from the confirmed values.

  schema.py     the fixed JSON schema + the sample sheet's ground truth
  prompt.py     the extraction instructions given to the model
  extractor.py  image preprocessing, the API call, parsing, validation
  store.py      saves the original photo + raw / extracted / confirmed JSON

Optional dependency: `pip install -r requirements-vision.txt` and an
ANTHROPIC_API_KEY. Without them the page shows an install hint; nothing
else in the app is affected.
"""
