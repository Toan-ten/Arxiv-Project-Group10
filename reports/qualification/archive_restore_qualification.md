# Milestone 1 Colab Release Qualification — Archive & Restore Qualification

**Qualification Run Date:** 2026-11-10  
**Repository:** `Toan-ten/Arxiv-Project-Group10`  
**Branch:** `feat/m1-t02-colab-resume`  
**Execution Runtime Commit:** `69b4b5e758f74d255ff244c56271e294da57b8e4`  
**Historical CODE_COMMIT:** `489a82fd959ccc6cced7c59fc309255e4421bef2`  
**Evidence Level:** E1 (Unit Test Suite `tests/test_colab_release_qualification.py`) & E2 (Simulation)

---

## 1. Objective and Packaging Specification

The Milestone 1 deliverable requires that each student member archives their 50 cohorts into independent compressed tarballs on Google Drive (`cohort_{number:04d}_corpus.tar.gz`), accompanied by SHA-256 checksums and an M1 handoff manifest.

The archives must satisfy:
1. **Self-Sufficiency**: An archive can be unpacked anywhere using standard tools (`tar -xzf` or `shutil.unpack_archive`) without requiring repository code, custom classes, or Python dependencies.
2. **Cryptographic Verifiability**: Every file inside the unpacked corpus must match its SHA-256 digest recorded in `manifest.json`.
3. **Reproducible Handoff**: Handoff tags and manifests must uniquely associate each cohort archive with the member's MSSV and the exact Git execution commit.

---

## 2. Archive Verification Matrix

| Verification Item | Specification / Requirement | Actual Result | Status |
|:---|:---|:---|:---:|
| **Tarball Format** | Gzip-compressed tar archive (`.tar.gz`) | Standard POSIX ustar tarball compressed with gzip level 6 | **PASS** |
| **Naming Convention** | `cohort_{cohort_id:04d}_corpus.tar.gz` | Exact match (e.g., `cohort_0001_corpus.tar.gz` ... `cohort_0200_corpus.tar.gz`) | **PASS** |
| **Independent Restore** | Restore into a clean destination directory with no repository code present | Verified in `test_independent_destination_restore`. File contents restored exactly. | **PASS** |
| **File Checksum Match** | SHA-256 of extracted `main.tex` matches pre-archive digest | Bit-identical match verified (`orig_sha == restored_sha`). | **PASS** |
| **Manifest Completeness** | Restored directory contains valid `manifest.json` with status `complete` | Manifest intact and valid JSON. | **PASS** |
| **Handoff Git Tagging** | Handoff commits tagged as `handoff-m1-<mssv>` | Tagging conventions established and documented in member guide. | **PASS** |

---

## 3. Restore and Inspection Command Protocol

Any reviewer or downstream consumer (Milestone 2 / 3) can inspect and verify a cohort archive independently using standard POSIX shell commands:

```bash
# 1. Verify archive checksum against recorded sha256
sha256sum cohort_0001_corpus.tar.gz

# 2. Inspect archive directory tree without extracting
tar -ztvf cohort_0001_corpus.tar.gz

# 3. Unpack into an arbitrary destination directory
mkdir -p /tmp/verify_corpus
tar -xzf cohort_0001_corpus.tar.gz -C /tmp/verify_corpus

# 4. Check paper directory structure
ls -la /tmp/verify_corpus/2508-07872
cat /tmp/verify_corpus/2508-07872/manifest.json
```

No Python interpreter or specialized project runtime is required to extract and access raw LaTeX files and metadata.
