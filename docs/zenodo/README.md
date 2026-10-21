# Zenodo deposit of the documentation (maintainers)

This folder is not published (`docs/conf.py` excludes it). It holds the one source of the documentation's
citation and image credits, and this page says how a version of the documentation is archived on Zenodo.

**What is archived**: the user documentation only, as a PDF and as a zip of the HTML pages built from the same
sources. Never the software, its installers or its source code. Licence of the record: CC BY 4.0
(`zenodo_metadata.json`); figures with third-party content keep their own terms (`image_credits.csv`).

## Files

| File | What it is |
| --- | --- |
| `zenodo_metadata.json` | The record metadata (Zenodo deposit API format). The build fills in `version` and `publication_date`. |
| `doi.json` | The DOIs Zenodo assigned: `concept_doi` (all versions) and `versions` (`"0.3.2": "10.5281/zenodo.N"`). Empty until Zenodo assigns them: never invent one. |
| `image_credits.csv` | One row per image under `docs/images/`: third-party sources (`;`-separated keys), whether the credit is drawn in the frame, notes. Rows marked "to confirm" need checking before the next deposit. |
| `credit_sources.csv` | Credit line, terms and link of each source key. |
| `citation.py` | Makes the APA / BibTeX / RIS citations and the credits tables (`docs/generated/`, included by `cite.md` and `image-credits.md`). Called by `conf.py` on every build; the build fails if a published image has no row in `image_credits.csv`. |

## Depositing a version

1. Check the free disk space (`df -h`): the build needs about 1 GB while it runs and stops by itself below 15 GB
   free. The deposit itself is about 35 MB (limit 300 MB).
2. On Zenodo (signed in as the LabShope Technologies account), start a **new upload**, or for a later release
   open the record and choose **New version**. Upload nothing yet. Click **Reserve DOI**.
3. Write the DOIs into `doi.json`:
   - the reserved DOI under `versions` with the version of `package.json` as the key;
   - on the first deposit only, `concept_doi`: `10.5281/zenodo.<conceptrecid>`, where `conceptrecid` is shown in
     the draft's API answer (`GET /api/deposit/depositions/<id>`). On later versions it stays the same.
4. Commit `doi.json`, then build from that commit:

   ```sh
   python scripts/release/build_docs_deposit.py --out ~/Downloads/radarx_docs_doi
   ```

   It writes the PDF, the HTML zip, `zenodo_metadata.json` and `SHA256SUMS`, using the commit time as the date
   (`SOURCE_DATE_EPOCH`), and deletes its intermediate `_build` folder.
5. Look at the PDF: the title page, the contents, *How to cite* (the DOI is the real one, no placeholder note)
   and *Images and third-party credits*.
6. Upload the PDF and the zip to the draft, copy the metadata from the generated `zenodo_metadata.json` (or send
   it with `PUT /api/deposit/depositions/<id>`), check the preview, and **Publish**. Publishing cannot be undone:
   files of a published version cannot be changed, only replaced by a new version.

## Before the first deposit

- Confirm the rows of `image_credits.csv` marked "to confirm" (origin of the QC, CPOL and NPOL sample files and of
  the basin polygons).
- The EOX Sentinel-2 cloudless imagery is CC BY-NC-SA 4.0: figures containing it are under those terms, not
  under the record's CC BY 4.0. The record description and the credits chapter say so; keep it that way, or
  replace those figures with *Realistic ground* off.
