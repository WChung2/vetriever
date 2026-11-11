"""Docling on the sealed papers with the GPU off. Runs in its own environment:

    HF_HUB_OFFLINE=1 <docling venv>/bin/python scripts/tablebench_docling.py

Settings are frozen for the benchmark: accurate table structure, no OCR, CPU, 8 threads.
Writes corpus/tablebench/out/docling/<PMCID>.md and times.csv. Prints counts only.
"""
import csv
import time
from pathlib import Path

from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
from docling.document_converter import DocumentConverter, PdfFormatOption

ROOT = Path("corpus/tablebench")
options = PdfPipelineOptions(do_table_structure=True, do_ocr=False)
options.table_structure_options.mode = TableFormerMode.ACCURATE
options.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=8)
converter = DocumentConverter(
    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
out = ROOT / "out" / "docling"
out.mkdir(parents=True, exist_ok=True)
done = failed = 0
with (out / "times.csv").open("a", newline="") as f:
    log = csv.writer(f)
    for paper in csv.DictReader((ROOT / "sealed.csv").open()):
        pmc = paper["pmcid"]
        if (out / f"{pmc}.md").exists():
            continue
        start = time.time()
        try:
            result = converter.convert(str(ROOT / "pdf" / f"{pmc}.pdf"))
            (out / f"{pmc}.md").write_text(result.document.export_to_markdown())
            done += 1
            status = "ok"
        except Exception as e:  # noqa: BLE001 - one paper failing must not stop the rest
            failed += 1
            status = type(e).__name__
        log.writerow([pmc, round(time.time() - start, 1), status])
        f.flush()
print("read", done, "failed", failed)
