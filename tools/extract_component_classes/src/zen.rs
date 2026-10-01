//! A cooked package's header, as IoStore stores it (`FZenPackageSummary`): a
//! 52-byte summary (versioning flag, header size, package name, package flags,
//! cooked header size, seven region offsets), the name batch, then an `i64` bulk
//! data map size and that many bytes. Only the 52-byte summary is accepted; a
//! 44-byte one fails the offset-order or hash-version check.
//! The offsets must be in order and inside the header; the name and bulk data
//! maps must end exactly at the first region; the public-hash, import and export
//! maps must hold whole records; the imported names must end exactly at the
//! header end. The bundle and dependency regions are only order-checked.

use crate::names::{MappedName, read_name_batch, with_number};
use crate::reader::{Cursor, Result, fail};

pub const SUMMARY_SIZE: usize = 52;
pub const EXPORT_ENTRY_SIZE: usize = 72;

#[derive(Debug, Clone, Copy)]
pub struct ExportEntry {
    pub object_name: MappedName,
    pub outer_index: u64,
    pub class_index: u64,
    pub super_index: u64,
    pub public_export_hash: u64,
}

#[derive(Debug, Clone)]
pub struct PackageHeader {
    pub name: String,
    pub names: Vec<String>,
    pub imported_public_export_hashes: Vec<u64>,
    pub exports: Vec<ExportEntry>,
    pub imported_package_names: Vec<String>,
}

/// The header size a package declares, from its first eight bytes. The caller
/// uses it to decide how much of the chunk to decompress.
pub fn declared_header_size(bytes: &[u8]) -> Result<u32> {
    let mut c = Cursor::new(bytes, "package summary");
    let versioning = c.u32()?;
    if versioning != 0 {
        return fail(format!(
            "package summary: versioning info flag {versioning}; cooked packages without it are the only kind checked"
        ));
    }
    c.u32()
}

/// Parse the header of a package from the start of its `ExportBundleData`
/// chunk. `bytes` must hold at least the declared header size.
pub fn parse_package_header(bytes: &[u8]) -> Result<PackageHeader> {
    let header_size = declared_header_size(bytes)?;
    if (header_size as usize) > bytes.len() {
        return fail(format!(
            "package summary: header size {header_size} but only {} bytes supplied",
            bytes.len()
        ));
    }
    let bytes = &bytes[..header_size as usize];
    let mut c = Cursor::new(bytes, "package summary");
    c.skip(8)?;
    let name = MappedName::read(&mut c)?;
    let _package_flags = c.u32()?;
    let _cooked_header_size = c.u32()?;
    let mut offsets = [0usize; 7];
    for slot in offsets.iter_mut() {
        let v = c.i32()?;
        if v < 0 {
            return fail(format!("package summary: negative offset {v}"));
        }
        *slot = v as usize;
    }
    let [
        public_hashes_at,
        import_map_at,
        export_map_at,
        bundle_entries_at,
        dep_headers_at,
        dep_entries_at,
        imported_names_at,
    ] = offsets;
    debug_assert_eq!(c.pos(), SUMMARY_SIZE);
    let mut previous = SUMMARY_SIZE;
    for (label, at) in [
        ("imported public export hashes", public_hashes_at),
        ("import map", import_map_at),
        ("export map", export_map_at),
        ("export bundle entries", bundle_entries_at),
        ("dependency bundle headers", dep_headers_at),
        ("dependency bundle entries", dep_entries_at),
        ("imported package names", imported_names_at),
    ] {
        if at < previous || at > bytes.len() {
            return fail(format!(
                "package summary: {label} offset {at} is out of order (after {previous}, header {})",
                bytes.len()
            ));
        }
        previous = at;
    }

    let names = read_name_batch(&mut c)?;
    let bulk_size = c.i64()?;
    if bulk_size < 0 {
        return fail(format!(
            "package summary: negative bulk data map size {bulk_size}"
        ));
    }
    c.skip(bulk_size as usize)?;
    if c.pos() != public_hashes_at {
        return fail(format!(
            "package summary: name map and bulk data map end at {}, but the next region starts at {public_hashes_at}",
            c.pos()
        ));
    }

    let imported_public_export_hashes = u64_array(
        bytes,
        public_hashes_at,
        import_map_at,
        "public export hashes",
    )?;
    // Not used here, but read so its size is checked like every other region.
    u64_array(bytes, import_map_at, export_map_at, "import map")?;

    let export_bytes = export_map_at..bundle_entries_at;
    if export_bytes.len() % EXPORT_ENTRY_SIZE != 0 {
        return fail(format!(
            "package summary: export map is {} bytes, not a multiple of {EXPORT_ENTRY_SIZE}",
            export_bytes.len()
        ));
    }
    let mut ec = Cursor::at(&bytes[..bundle_entries_at], export_map_at, "export map")?;
    let mut exports = Vec::with_capacity(export_bytes.len() / EXPORT_ENTRY_SIZE);
    while ec.remaining() > 0 {
        let _serial_offset = ec.u64()?;
        let _serial_size = ec.u64()?;
        let object_name = MappedName::read(&mut ec)?;
        let outer_index = ec.u64()?;
        let class_index = ec.u64()?;
        let super_index = ec.u64()?;
        let _template_index = ec.u64()?;
        let public_export_hash = ec.u64()?;
        let _object_flags = ec.u32()?;
        let _filter_flags = ec.u8()?;
        ec.skip(3)?;
        object_name.base(&names)?;
        exports.push(ExportEntry {
            object_name,
            outer_index,
            class_index,
            super_index,
            public_export_hash,
        });
    }

    let mut nc = Cursor::at(bytes, imported_names_at, "imported package names")?;
    let imported_base = read_name_batch(&mut nc)?;
    let mut imported_package_names = Vec::with_capacity(imported_base.len());
    for base in &imported_base {
        let number = nc.u32()?;
        imported_package_names.push(with_number(base, number));
    }
    if nc.remaining() != 0 {
        return fail(format!(
            "package summary: {} bytes after the imported package names, before the header ends",
            nc.remaining()
        ));
    }

    let rendered = name.render(&names)?;
    Ok(PackageHeader {
        name: rendered,
        names,
        imported_public_export_hashes,
        exports,
        imported_package_names,
    })
}

fn u64_array(bytes: &[u8], start: usize, end: usize, what: &'static str) -> Result<Vec<u64>> {
    if (end - start) % 8 != 0 {
        return fail(format!(
            "package summary: {what} is {} bytes, not a multiple of 8",
            end - start
        ));
    }
    let mut c = Cursor::at(&bytes[..end], start, what)?;
    let mut out = Vec::with_capacity((end - start) / 8);
    while c.remaining() > 0 {
        out.push(c.u64()?);
    }
    Ok(out)
}

impl PackageHeader {
    /// An export's name as the wire spells it, instance number included.
    pub fn export_name(&self, export: &ExportEntry) -> String {
        // Checked in `parse_package_header`, so this cannot fail.
        export
            .object_name
            .render(&self.names)
            .unwrap_or_else(|_| "?".to_owned())
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::names::tests::name_batch;

    /// One synthetic export: `(name index, name number, outer, class, super,
    /// public export hash)`.
    pub type ExportSpec = (u32, u32, u64, u64, u64, u64);

    #[derive(Default)]
    pub struct PackageSpec<'a> {
        pub names: Vec<&'a str>,
        pub package_name: u32,
        pub exports: Vec<ExportSpec>,
        pub imported_hashes: Vec<u64>,
        pub imported_packages: Vec<(&'a str, u32)>,
    }

    /// Serialize a package header the way the 13.06 containers lay one out.
    pub fn build_package(spec: &PackageSpec<'_>) -> Vec<u8> {
        let mut names = name_batch(&spec.names);
        names.extend_from_slice(&0i64.to_le_bytes());
        let public_hashes_at = SUMMARY_SIZE + names.len();
        let import_map_at = public_hashes_at + spec.imported_hashes.len() * 8;
        let import_map: Vec<u64> = vec![u64::MAX];
        let export_map_at = import_map_at + import_map.len() * 8;
        let bundle_at = export_map_at + spec.exports.len() * EXPORT_ENTRY_SIZE;
        let dep_headers_at = bundle_at + spec.exports.len() * 16;
        let dep_entries_at = dep_headers_at;
        let imported_names_at = dep_entries_at;
        let imported_names: Vec<&str> = spec.imported_packages.iter().map(|p| p.0).collect();
        let mut imported = name_batch(&imported_names);
        for (_, number) in &spec.imported_packages {
            imported.extend_from_slice(&number.to_le_bytes());
        }
        let header_size = imported_names_at + imported.len();

        let mut out = Vec::new();
        out.extend_from_slice(&0u32.to_le_bytes());
        out.extend_from_slice(&(header_size as u32).to_le_bytes());
        out.extend_from_slice(&spec.package_name.to_le_bytes());
        out.extend_from_slice(&0u32.to_le_bytes());
        out.extend_from_slice(&0x8000_2200u32.to_le_bytes());
        out.extend_from_slice(&(header_size as u32 + 100).to_le_bytes());
        for at in [
            public_hashes_at,
            import_map_at,
            export_map_at,
            bundle_at,
            dep_headers_at,
            dep_entries_at,
            imported_names_at,
        ] {
            out.extend_from_slice(&(at as i32).to_le_bytes());
        }
        assert_eq!(out.len(), SUMMARY_SIZE);
        out.extend_from_slice(&names);
        for h in &spec.imported_hashes {
            out.extend_from_slice(&h.to_le_bytes());
        }
        for i in &import_map {
            out.extend_from_slice(&i.to_le_bytes());
        }
        for (name, number, outer, class, sup, hash) in &spec.exports {
            out.extend_from_slice(&0u64.to_le_bytes());
            out.extend_from_slice(&0u64.to_le_bytes());
            out.extend_from_slice(&name.to_le_bytes());
            out.extend_from_slice(&number.to_le_bytes());
            for v in [*outer, *class, *sup, u64::MAX, *hash] {
                out.extend_from_slice(&v.to_le_bytes());
            }
            out.extend_from_slice(&0x9u32.to_le_bytes());
            out.extend_from_slice(&[0, 0, 0, 0]);
        }
        out.extend(std::iter::repeat_n(0u8, spec.exports.len() * 16));
        out.extend_from_slice(&imported);
        assert_eq!(out.len(), header_size);
        // Export data follows the header in a real chunk.
        out.extend_from_slice(&[0xEE; 32]);
        out
    }

    fn sample() -> PackageSpec<'static> {
        PackageSpec {
            names: vec!["/Game/BP_Thing", "BP_Thing_C", "Zoom_GEN_VARIABLE"],
            package_name: 0,
            exports: vec![(1, 0, u64::MAX, 7, 8, 0x1234), (2, 0, 0, 9, u64::MAX, 0)],
            imported_hashes: vec![0xABCD],
            imported_packages: vec![("/Game/Parent", 0), ("/Game/Other", 3)],
        }
    }

    #[test]
    fn a_synthetic_header_parses_field_for_field() {
        let bytes = build_package(&sample());
        let pkg = parse_package_header(&bytes).unwrap();
        assert_eq!(pkg.name, "/Game/BP_Thing");
        assert_eq!(pkg.exports.len(), 2);
        assert_eq!(pkg.export_name(&pkg.exports[1]), "Zoom_GEN_VARIABLE");
        assert_eq!(pkg.exports[0].class_index, 7);
        assert_eq!(pkg.exports[0].super_index, 8);
        assert_eq!(pkg.exports[0].public_export_hash, 0x1234);
        assert_eq!(pkg.exports[1].outer_index, 0);
        assert_eq!(pkg.imported_public_export_hashes, [0xABCD]);
        assert_eq!(
            pkg.imported_package_names,
            ["/Game/Parent", "/Game/Other_2"]
        );
    }

    #[test]
    fn the_header_size_bounds_what_is_read() {
        let bytes = build_package(&sample());
        let size = declared_header_size(&bytes).unwrap() as usize;
        assert!(parse_package_header(&bytes[..size]).is_ok());
        assert!(
            parse_package_header(&bytes[..size - 1])
                .unwrap_err()
                .0
                .contains("only")
        );
    }

    /// A 44-byte summary puts the name batch eight bytes earlier: read as 52
    /// bytes, the batch's first two words become the last two offsets, which
    /// the order check refuses.
    #[test]
    fn a_summary_of_the_other_width_is_refused() {
        let bytes = build_package(&sample());
        let mut shifted = bytes[..44].to_vec();
        shifted.extend_from_slice(&bytes[SUMMARY_SIZE..]);
        let size = (declared_header_size(&bytes).unwrap() - 8).to_le_bytes();
        shifted[4..8].copy_from_slice(&size);
        assert!(parse_package_header(&shifted).is_err());
    }

    #[test]
    fn a_region_that_does_not_meet_the_next_offset_is_refused() {
        let mut bytes = build_package(&sample());
        // Move the imported-public-export-hash offset (the first of the seven,
        // after 24 bytes of fixed fields) one byte later.
        let at = 24;
        let v = i32::from_le_bytes(bytes[at..at + 4].try_into().unwrap()) + 1;
        bytes[at..at + 4].copy_from_slice(&v.to_le_bytes());
        assert!(parse_package_header(&bytes).is_err());
    }

    /// Refused by the order check, not a panic in `u64_array`: a scan worker's
    /// panic would end the run with no row and no summary.
    #[test]
    fn an_offset_past_the_header_end_is_refused_not_a_panic() {
        let mut bytes = build_package(&sample());
        let header_size = declared_header_size(&bytes).unwrap() as i32;
        // The import map offset, second of the seven, moved by a multiple of 8
        // so only the order check stands between it and an out-of-range slice.
        let at = 28;
        let v = i32::from_le_bytes(bytes[at..at + 4].try_into().unwrap());
        let past = v + 8 * (header_size / 8 + 1);
        bytes[at..at + 4].copy_from_slice(&past.to_le_bytes());
        let err = parse_package_header(&bytes).unwrap_err();
        assert!(err.0.contains("import map offset"), "{err}");
    }

    /// Every region after the name map is found through its own offset, so a
    /// bulk data map of the wrong size would go unnoticed without the explicit
    /// position check.
    #[test]
    fn a_bulk_data_map_that_overruns_the_next_region_is_refused() {
        let spec = sample();
        let mut bytes = build_package(&spec);
        let names_end = SUMMARY_SIZE + crate::names::tests::name_batch(&spec.names).len();
        bytes[names_end..names_end + 8].copy_from_slice(&8i64.to_le_bytes());
        let err = parse_package_header(&bytes).unwrap_err();
        assert!(err.0.contains("next region"), "{err}");
    }

    #[test]
    fn an_export_name_outside_the_name_map_is_refused() {
        let mut spec = sample();
        spec.exports[1].0 = 9;
        assert!(parse_package_header(&build_package(&spec)).is_err());
    }

    #[test]
    fn versioning_info_is_refused_by_name() {
        let mut bytes = build_package(&sample());
        bytes[0] = 1;
        assert!(
            parse_package_header(&bytes)
                .unwrap_err()
                .0
                .contains("versioning")
        );
    }
}
