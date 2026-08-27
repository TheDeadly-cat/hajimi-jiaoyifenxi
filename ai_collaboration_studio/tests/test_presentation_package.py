from __future__ import annotations

import copy
import hashlib
import unittest
import zipfile
from io import BytesIO
from unittest.mock import patch
from xml.sax.saxutils import escape

import backend.presentation_package as presentation_package_module
from backend.material_ingest import extract_material
from backend.presentation_package import (
    ARTIFACT_RENDER_PACKAGE_VERSION,
    ARTIFACT_RENDER_VERIFICATION_RECEIPT_VERSION,
    PPTX_CONTENT_TYPE,
    PPTX_INGEST_PACKAGE_VERSION,
    PresentationPackageError,
    build_artifact_render_package,
    build_artifact_render_verification_receipt,
    build_pptx_ingest_package,
    verify_artifact_render_package,
    verify_artifact_render_verification_receipt,
    verify_pptx_ingest_package,
)


def sha256(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _slide_xml(text: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree><p:sp><p:txBody>
    <a:p><a:r><a:t>{escape(text)}</a:t></a:r></a:p>
  </p:txBody></p:sp></p:spTree></p:cSld>
</p:sld>""".encode("utf-8")


def _notes_xml(text: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:notes xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
         xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree><p:sp><p:txBody>
    <a:p><a:r><a:t>{escape(text)}</a:t></a:r></a:p>
  </p:txBody></p:sp></p:spTree></p:cSld>
</p:notes>""".encode("utf-8")


def make_pptx(
    slide_texts: list[str] | None = None,
    *,
    notes: dict[int, str] | None = None,
    external_target: str = "",
    extra_members: dict[str, bytes] | None = None,
    presentation_content_type: str = PPTX_CONTENT_TYPE,
    slide_override: dict[int, bytes] | None = None,
) -> bytes:
    texts = slide_texts or ["Deck title"]
    notes = notes or {}
    extra_members = extra_members or {}
    slide_override = slide_override or {}
    content_type_rows = [
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        f'<Override PartName="/ppt/presentation.xml" ContentType="{presentation_content_type}"/>',
    ]
    presentation_ids = []
    presentation_relationships = []
    members: dict[str, bytes] = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            + "".join(content_type_rows)
            + "</Types>"
        ).encode("utf-8"),
        "_rels/.rels": b'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>''',
    }
    for index, text in enumerate(texts, start=1):
        relationship_id = f"rId{index}"
        presentation_ids.append(
            f'<p:sldId id="{255 + index}" r:id="{relationship_id}"/>'
        )
        presentation_relationships.append(
            f'<Relationship Id="{relationship_id}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
            f'Target="slides/slide{index}.xml"/>'
        )
        members[f"ppt/slides/slide{index}.xml"] = slide_override.get(
            index,
            _slide_xml(text),
        )
        slide_relationships = []
        if index in notes:
            slide_relationships.append(
                '<Relationship Id="rIdNotes" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" '
                f'Target="../notesSlides/notesSlide{index}.xml"/>'
            )
            members[f"ppt/notesSlides/notesSlide{index}.xml"] = _notes_xml(
                notes[index]
            )
        if index == 1 and external_target:
            slide_relationships.append(
                '<Relationship Id="rIdLink" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
                f'Target="{escape(external_target)}" TargetMode="External"/>'
            )
        if slide_relationships:
            members[f"ppt/slides/_rels/slide{index}.xml.rels"] = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                + "".join(slide_relationships)
                + "</Relationships>"
            ).encode("utf-8")
    members["ppt/presentation.xml"] = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<p:sldIdLst>'
        + "".join(presentation_ids)
        + "</p:sldIdLst></p:presentation>"
    ).encode("utf-8")
    members["ppt/_rels/presentation.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(presentation_relationships)
        + "</Relationships>"
    ).encode("utf-8")
    members.update(extra_members)

    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return output.getvalue()


def passing_checks() -> dict[str, bool]:
    return {
        "all_slides_rendered": True,
        "no_unintended_overflow": True,
        "no_unintended_overlap": True,
        "render_succeeded": True,
        "slide_count_matches": True,
    }


class PPTXIngestPackageTests(unittest.TestCase):
    def test_builds_ordered_hash_bound_package_with_notes_and_no_link_fetch(self) -> None:
        external_target = "https://example.invalid/private-deck-resource"
        raw = make_pptx(
            ["First slide", "Second slide"],
            notes={1: "Presenter-only note"},
            external_target=external_target,
        )

        first = build_pptx_ingest_package(raw, filename="briefing.pptx")
        second = build_pptx_ingest_package(raw, filename="briefing.pptx")

        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], PPTX_INGEST_PACKAGE_VERSION)
        self.assertEqual(first["source"]["source_sha256"], sha256(raw))
        self.assertEqual(first["presentation"]["slide_count"], 2)
        self.assertEqual(
            [item["text"] for item in first["presentation"]["slides"]],
            ["First slide", "Second slide"],
        )
        self.assertEqual(
            first["presentation"]["slides"][0]["notes_text"],
            "Presenter-only note",
        )
        self.assertEqual(first["presentation"]["external_relationship_count"], 1)
        self.assertFalse(first["safety"]["external_relationships_fetched"])
        self.assertNotIn(external_target, str(first))
        self.assertEqual(
            verify_pptx_ingest_package(first, source_bytes=raw),
            first,
        )

    def test_material_ingest_uses_the_safe_package_and_exposes_its_receipt(self) -> None:
        raw = make_pptx(["Quarterly briefing", "Evidence and risks"])

        extracted = extract_material(raw, "download", PPTX_CONTENT_TYPE)

        self.assertEqual(extracted.title, "Quarterly briefing")
        self.assertIn("# Slide 1", extracted.text)
        self.assertIn("Evidence and risks", extracted.text)
        self.assertEqual(extracted.metadata["extraction_method"], "pptx_zip_xml_v1")
        self.assertEqual(extracted.metadata["pptx_slide_count"], 2)
        package = extracted.metadata["pptx_ingest_package"]
        self.assertEqual(package["source"]["filename"], "download.pptx")
        self.assertEqual(package["package_sha256"], extracted.metadata["pptx_ingest_package_sha256"])
        self.assertEqual(verify_pptx_ingest_package(package, source_bytes=raw), package)

    def test_rejects_macros_ole_embedded_objects_and_macro_content_types(self) -> None:
        cases = {
            "macro member": make_pptx(extra_members={"ppt/vbaProject.bin": b"macro"}),
            "OLE embedding": make_pptx(extra_members={"ppt/embeddings/oleObject1.dat": b"ole"}),
            "macro content type": make_pptx(
                presentation_content_type="application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"
            ),
        }
        for label, raw in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(PresentationPackageError) as raised:
                    build_pptx_ingest_package(raw, filename="unsafe.pptx")
                self.assertEqual(raised.exception.code, "PPTX_ACTIVE_CONTENT_REJECTED")

    def test_rejects_xml_doctype_source_uncompressed_and_slide_limits(self) -> None:
        active_xml = b'''<?xml version="1.0"?>
<!DOCTYPE p:sld [<!ENTITY x "unsafe">]>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>'''
        with self.assertRaises(PresentationPackageError) as active:
            build_pptx_ingest_package(
                make_pptx(slide_override={1: active_xml}),
                filename="active.pptx",
            )
        self.assertEqual(active.exception.code, "PPTX_XML_ACTIVE_CONTENT_REJECTED")

        raw = make_pptx()
        with patch.object(presentation_package_module, "MAX_PPTX_SOURCE_BYTES", len(raw) - 1):
            with self.assertRaises(PresentationPackageError) as source_limit:
                build_pptx_ingest_package(raw, filename="large.pptx")
        self.assertEqual(source_limit.exception.code, "PPTX_SOURCE_LIMIT_EXCEEDED")

        with patch.object(presentation_package_module, "MAX_PPTX_UNCOMPRESSED_BYTES", 100):
            with self.assertRaises(PresentationPackageError) as expanded_limit:
                build_pptx_ingest_package(raw, filename="expanded.pptx")
        self.assertEqual(
            expanded_limit.exception.code,
            "PPTX_UNCOMPRESSED_LIMIT_EXCEEDED",
        )

        with patch.object(presentation_package_module, "MAX_PPTX_SLIDES", 1):
            with self.assertRaises(PresentationPackageError) as slide_limit:
                build_pptx_ingest_package(
                    make_pptx(["one", "two"]),
                    filename="too-many.pptx",
                )
        self.assertEqual(slide_limit.exception.code, "PPTX_SLIDE_LIMIT_EXCEEDED")

    def test_rejects_tampering_unknown_fields_and_wrong_source_binding(self) -> None:
        raw = make_pptx(["Bound source"])
        package = build_pptx_ingest_package(raw, filename="bound.pptx")

        tampered = copy.deepcopy(package)
        tampered["presentation"]["slides"][0]["text"] = "tampered"
        with self.assertRaises(PresentationPackageError) as changed:
            verify_pptx_ingest_package(tampered)
        self.assertEqual(changed.exception.code, "PPTX_PACKAGE_INTEGRITY_FAILED")

        unknown = copy.deepcopy(package)
        unknown["unexpected"] = True
        with self.assertRaises(PresentationPackageError):
            verify_pptx_ingest_package(unknown)

        with self.assertRaises(PresentationPackageError) as wrong_source:
            verify_pptx_ingest_package(package, source_bytes=raw + b"x")
        self.assertEqual(wrong_source.exception.code, "PPTX_SOURCE_BINDING_MISMATCH")


class ArtifactRenderPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.output = make_pptx(["Rendered one", "Rendered two"])
        self.package = build_artifact_render_package(
            artifact_id="artifact_1",
            artifact_version=3,
            artifact_snapshot_sha256=sha256("artifact snapshot"),
            output_filename="rendered.pptx",
            output_bytes=self.output,
            renderer_id="libreoffice",
            renderer_version="v1",
            rendered_slide_sha256s=[sha256("slide 1 png"), sha256("slide 2 png")],
        )

    def test_builds_and_verifies_hash_bound_render_package(self) -> None:
        self.assertEqual(self.package["schema_version"], ARTIFACT_RENDER_PACKAGE_VERSION)
        self.assertEqual(self.package["output"]["output_sha256"], sha256(self.output))
        self.assertEqual(self.package["output"]["slide_count"], 2)
        self.assertEqual(
            verify_artifact_render_package(self.package, output_bytes=self.output),
            self.package,
        )

        with self.assertRaises(PresentationPackageError) as wrong_output:
            verify_artifact_render_package(self.package, output_bytes=self.output + b"x")
        self.assertEqual(wrong_output.exception.code, "ARTIFACT_RENDER_OUTPUT_MISMATCH")

    def test_automatic_pass_without_user_acceptance_needs_review(self) -> None:
        receipt = build_artifact_render_verification_receipt(
            self.package,
            automatic_checks=passing_checks(),
            user_review={
                "status": "pending",
                "reviewed_by": "",
                "reviewed_at": 0,
                "notes": "",
            },
        )

        self.assertEqual(
            receipt["schema_version"],
            ARTIFACT_RENDER_VERIFICATION_RECEIPT_VERSION,
        )
        self.assertEqual(receipt["automatic_status"], "passed")
        self.assertEqual(receipt["status"], "needs_user_review")
        self.assertEqual(receipt["issues"], ["USER_REVIEW_REQUIRED"])
        self.assertEqual(
            verify_artifact_render_verification_receipt(
                receipt,
                render_package=self.package,
            ),
            receipt,
        )

    def test_only_explicit_user_acceptance_after_all_checks_is_verified(self) -> None:
        accepted = build_artifact_render_verification_receipt(
            self.package,
            automatic_checks=passing_checks(),
            user_review={
                "status": "accepted",
                "reviewed_by": "user",
                "reviewed_at": 1_800_000_000_000,
                "notes": "Reviewed every rendered slide.",
            },
        )
        self.assertEqual(accepted["status"], "verified")
        self.assertEqual(accepted["issues"], [])

        failed_checks = passing_checks()
        failed_checks["no_unintended_overlap"] = False
        failed = build_artifact_render_verification_receipt(
            self.package,
            automatic_checks=failed_checks,
            user_review={
                "status": "accepted",
                "reviewed_by": "user",
                "reviewed_at": 1_800_000_000_001,
                "notes": "Acceptance cannot override a failed gate.",
            },
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(
            failed["issues"],
            ["AUTOMATIC_CHECK_FAILED:no_unintended_overlap"],
        )

    def test_receipt_status_and_package_tampering_fail_closed(self) -> None:
        receipt = build_artifact_render_verification_receipt(
            self.package,
            automatic_checks=passing_checks(),
            user_review={
                "status": "pending",
                "reviewed_by": "",
                "reviewed_at": 0,
                "notes": "",
            },
        )
        tampered_receipt = copy.deepcopy(receipt)
        tampered_receipt["status"] = "verified"
        with self.assertRaises(PresentationPackageError) as receipt_error:
            verify_artifact_render_verification_receipt(tampered_receipt)
        self.assertEqual(
            receipt_error.exception.code,
            "ARTIFACT_RENDER_RECEIPT_INTEGRITY_FAILED",
        )

        tampered_package = copy.deepcopy(self.package)
        tampered_package["output"]["slide_count"] = 1
        with self.assertRaises(PresentationPackageError) as package_error:
            verify_artifact_render_package(tampered_package)
        self.assertEqual(
            package_error.exception.code,
            "ARTIFACT_RENDER_PACKAGE_INVALID",
        )

    def test_render_package_rejects_missing_rendered_slide_and_non_user_review(self) -> None:
        with self.assertRaises(PresentationPackageError) as mismatch:
            build_artifact_render_package(
                artifact_id="artifact_1",
                artifact_version=1,
                artifact_snapshot_sha256=sha256("artifact"),
                output_filename="rendered.pptx",
                output_bytes=self.output,
                renderer_id="renderer",
                renderer_version="v1",
                rendered_slide_sha256s=[sha256("only one")],
            )
        self.assertEqual(
            mismatch.exception.code,
            "ARTIFACT_RENDER_SLIDE_COUNT_MISMATCH",
        )

        with self.assertRaises(PresentationPackageError):
            build_artifact_render_verification_receipt(
                self.package,
                automatic_checks=passing_checks(),
                user_review={
                    "status": "accepted",
                    "reviewed_by": "automation",
                    "reviewed_at": 1,
                    "notes": "",
                },
            )


if __name__ == "__main__":
    unittest.main()
