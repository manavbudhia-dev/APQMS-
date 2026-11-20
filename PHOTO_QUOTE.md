# Photo Quote — photo of a hand-edited quotation sheet → quotation

Sidebar → **Photo Quote**. Upload a photo of the printed sheet with handwritten edits. Claude reads the final values (struck-out values ignored, handwritten replacements used), you check them, and the app makes a normal quotation with its PDF.

## Setup

```bash
pip install -r requirements-vision.txt      # anthropic SDK + Pillow
```
Set `ANTHROPIC_API_KEY` in the server environment (cPanel: *Setup Python App → Environment variables*), then restart.

Optional settings: `APQMS_VISION_MODEL` (default `claude-opus-5-5`), `APQMS_VISION_EFFORT` (default `high`).

## Flow

1. **Upload**: JPG, PNG or WebP, up to 20 MB. The phone camera opens directly. Each user can run 30 extractions per hour.
2. **Preprocess** (Pillow): auto-rotate from EXIF, light autocontrast, downscale to a 2400 px long edge. There is **no auto-crop**, because a bad crop could cut off a handwritten edit. The prompt tells the model to ignore text at the edges instead.
3. **Extract**: one Messages API call with the image as base64. The JSON schema is enforced through structured outputs (`output_config.format`). Fallbacks are on, so a declined request is retried on Anthropic's recommended model.
4. **Validate** (`app/vision/extractor.py`):
   - Strips code fences and retries once on invalid JSON.
   - Numbers must be numbers. Values like `"8,360/-"` are converted, but flagged.
   - Required fields: `rate_per_mtr`, `quantity_mtrs`, `gst_percent`, `delivery_weeks`, `payment_terms`.
   - Sanity checks (unusual GST rate, delivery > 52 weeks, rate ≤ 0) only add flags. Nothing is guessed or auto-fixed.
5. **Review screen**: the photo and the editable values side by side.
   - Every `needs_review` item is highlighted yellow with the model's reason.
   - **Rate, quantity and delivery each need a "Maine photo se milaya" tick**, and the server rejects the form without them.
   - You pick the customer.
   - If extraction failed, you can still type the values by hand.
6. **Create**: one quotation line (product name plus specification lines for link, pin, attachment and width), terms for GST, price basis, delivery and payment, and the handwritten note as the customer-visible note. The PDF uses the existing quotation template. A double-click cannot create two quotations.

## Audit trail

Table `vision_extractions` stores:
- the original photo (exact bytes, in `vision_uploads/` next to the database, mode 0600) and its SHA-256
- the model, number of attempts and token usage
- the raw model output and the validated extraction
- the values the user confirmed, who confirmed them and when
- the quotation that was created

The quotation's internal note records the rate change (e.g. "revised from 7600 to 8360").

## Checking accuracy on real photos

```bash
export ANTHROPIC_API_KEY=...
python3 tools/eval_vision.py sheet.jpg                 # compares against the sample sheet from the brief
python3 tools/eval_vision.py sheet.jpg --runs 3        # stability: same photo 3 times
python3 tools/eval_vision.py *.jpg --truth truth.json --save results/
```
- Numeric fields must match exactly. Text fields pass if they are close.
- Each run is one paid API call.
- Try imperfect photos too: tilted, shadowed, other papers at the edges.

## Notes

- **No `temperature: 0`**: current Opus models reject sampling parameters (HTTP 400). Consistency comes from the enforced schema, validation and human confirmation. Use `--runs 3` to measure stability.
- **Customer names**: the sheet has no customer field, so the customer is chosen on the review screen.
- **Hindi / Punjabi notes**: the prompt asks the model to translate handwritten notes into English for the `notes` field.
- **Files**: `app/vision/` (schema, prompt, extractor, store), `app/views/vision_pages.py`, `tests/test_vision.py` (fake API client, no network), `tools/eval_vision.py`.
