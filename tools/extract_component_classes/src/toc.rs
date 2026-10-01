//! The `.utoc` table of contents. Layout, in file order: a 144-byte header; a
//! 12-byte chunk id and a 10-byte offset/length per chunk; perfect-hash seeds
//! and the overflow list; a 12-byte entry per compression block; the method
//! names; the signature block if signed; the directory index; a 33-byte meta
//! record per chunk.
//!
//! Only TOC version 5 (every 13.06 container, read to its last byte) is
//! accepted: version 8 changes the meta size and versions below 5 lack the
//! overflow list, either of which would misplace the directory index silently.

use crate::reader::{Cursor, Result, fail};

pub const TOC_MAGIC: &[u8; 16] = b"-==--==--==--==-";
pub const SUPPORTED_TOC_VERSION: u8 = 5;
const TOC_HEADER_SIZE: u32 = 144;
const COMPRESSED_BLOCK_ENTRY_SIZE: u32 = 12;
/// `FIoStoreTocEntryMeta` before TOC version 8: a 32-byte hash and a flag byte.
const META_SIZE: usize = 33;

pub const FLAG_ENCRYPTED: u8 = 2;
pub const FLAG_SIGNED: u8 = 4;
pub const FLAG_INDEXED: u8 = 8;

/// `EIoChunkType` values this tool looks at.
pub const CHUNK_EXPORT_BUNDLE_DATA: u8 = 1;
pub const CHUNK_SCRIPT_OBJECTS: u8 = 5;

#[derive(Debug, Clone, Copy)]
pub struct ChunkId {
    pub id: u64,
    pub chunk_type: u8,
}

#[derive(Debug, Clone, Copy)]
pub struct OffsetLength {
    pub offset: u64,
    pub length: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CompressedBlock {
    /// Byte offset of the block in the `.ucas`.
    pub offset: u64,
    pub compressed_size: u32,
    pub uncompressed_size: u32,
    /// 0 = stored; otherwise 1-based into `Toc::methods`.
    pub method: u8,
}

#[derive(Debug, Clone)]
pub struct Toc {
    pub container_id: u64,
    /// Bytes of the uncompressed stream per compression block.
    pub block_size: u32,
    pub chunk_ids: Vec<ChunkId>,
    pub chunks: Vec<OffsetLength>,
    pub blocks: Vec<CompressedBlock>,
    pub methods: Vec<String>,
    pub directory_index: Vec<u8>,
}

impl Toc {
    /// The compression method name for a block, `None` for a stored block.
    pub fn method_name(&self, method: u8) -> Result<Option<&str>> {
        if method == 0 {
            return Ok(None);
        }
        match self.methods.get(usize::from(method) - 1) {
            Some(m) => Ok(Some(m.as_str())),
            None => fail(format!(
                "compression method {method} out of range ({} declared)",
                self.methods.len()
            )),
        }
    }
}

pub fn parse_toc(bytes: &[u8]) -> Result<Toc> {
    let mut c = Cursor::new(bytes, "utoc");
    let magic = c.take(16)?;
    if magic != TOC_MAGIC {
        return fail("utoc: bad magic");
    }
    let version = c.u8()?;
    c.skip(3)?;
    if version != SUPPORTED_TOC_VERSION {
        return fail(format!(
            "utoc: TOC version {version}; only version {SUPPORTED_TOC_VERSION} has been checked"
        ));
    }
    let header_size = c.u32()?;
    let entry_count = c.u32()?;
    let compressed_block_count = c.u32()?;
    let compressed_block_entry_size = c.u32()?;
    let compression_method_count = c.u32()?;
    let compression_method_length = c.u32()?;
    let compression_block_size = c.u32()?;
    let directory_index_size = c.u32()?;
    let partition_count = c.u32()?;
    let container_id = c.u64()?;
    let encryption_guid = c.take(16)?;
    let container_flags = c.u8()?;
    c.skip(3)?;
    let perfect_hash_seed_count = c.u32()?;
    let _partition_size = c.u64()?;
    let chunks_without_perfect_hash = c.u32()?;
    c.skip(4 + 5 * 8)?;
    if header_size != TOC_HEADER_SIZE || c.pos() != TOC_HEADER_SIZE as usize {
        return fail(format!(
            "utoc: header size {header_size}, expected {TOC_HEADER_SIZE}"
        ));
    }
    if compressed_block_entry_size != COMPRESSED_BLOCK_ENTRY_SIZE {
        return fail(format!(
            "utoc: compressed block entry size {compressed_block_entry_size}, expected {COMPRESSED_BLOCK_ENTRY_SIZE}"
        ));
    }
    if container_flags & FLAG_ENCRYPTED != 0 {
        return fail("utoc: container is encrypted; this tool does not decrypt");
    }
    if encryption_guid.iter().any(|&b| b != 0) {
        return fail("utoc: nonzero encryption key GUID on an unencrypted container");
    }
    if partition_count > 1 {
        return fail(format!(
            "utoc: {partition_count} partitions; only single-partition containers are read"
        ));
    }
    if compression_block_size == 0 {
        return fail("utoc: compression block size 0");
    }

    let n = entry_count as usize;
    let mut chunk_ids = Vec::with_capacity(n.min(c.remaining() / 12));
    for _ in 0..n {
        let id = c.u64()?;
        let _index = c.be_uint(2)?;
        c.skip(1)?;
        let chunk_type = c.u8()?;
        chunk_ids.push(ChunkId { id, chunk_type });
    }
    let mut chunks = Vec::with_capacity(n.min(c.remaining() / 10));
    for _ in 0..n {
        let offset = c.be_uint(5)?;
        let length = c.be_uint(5)?;
        chunks.push(OffsetLength { offset, length });
    }
    c.skip((perfect_hash_seed_count as usize).saturating_mul(4))?;
    c.skip((chunks_without_perfect_hash as usize).saturating_mul(4))?;

    let nb = compressed_block_count as usize;
    let mut blocks = Vec::with_capacity(nb.min(c.remaining() / 12));
    for _ in 0..nb {
        let offset = c.le_uint(5)?;
        let compressed_size = c.le_uint(3)? as u32;
        let uncompressed_size = c.le_uint(3)? as u32;
        let method = c.u8()?;
        blocks.push(CompressedBlock {
            offset,
            compressed_size,
            uncompressed_size,
            method,
        });
    }

    let mut methods = Vec::with_capacity(compression_method_count as usize);
    for _ in 0..compression_method_count {
        let raw = c.take(compression_method_length as usize)?;
        let end = raw.iter().position(|&b| b == 0).unwrap_or(raw.len());
        methods.push(String::from_utf8_lossy(&raw[..end]).into_owned());
    }

    if container_flags & FLAG_SIGNED != 0 {
        let hash_size = c.i32()?;
        if hash_size < 0 {
            return fail(format!("utoc: negative signature size {hash_size}"));
        }
        // TOC signature, block signature, then one SHA-1 per block.
        c.skip((hash_size as usize).saturating_mul(2))?;
        c.skip(nb.saturating_mul(20))?;
    }

    let mut directory_index = Vec::new();
    if container_flags & FLAG_INDEXED != 0 && directory_index_size > 0 {
        directory_index = c.take(directory_index_size as usize)?.to_vec();
    } else if directory_index_size > 0 {
        // Present but not flagged: still occupies its bytes.
        c.skip(directory_index_size as usize)?;
    }

    let meta = n.saturating_mul(META_SIZE);
    if c.remaining() != meta {
        return fail(format!(
            "utoc: {} bytes after the directory index, expected {} ({} chunks x {META_SIZE}-byte meta); the layout does not match",
            c.remaining(),
            meta,
            n
        ));
    }

    let toc = Toc {
        container_id,
        block_size: compression_block_size,
        chunk_ids,
        chunks,
        blocks,
        methods,
        directory_index,
    };
    for (i, b) in toc.blocks.iter().enumerate() {
        toc.method_name(b.method)
            .map_err(|e| crate::reader::Error(format!("utoc: block {i}: {e}")))?;
    }
    let cbs = u64::from(compression_block_size);
    for (i, ch) in toc.chunks.iter().enumerate() {
        if ch.length == 0 {
            continue;
        }
        let last = (ch.offset + ch.length - 1) / cbs;
        if last >= toc.blocks.len() as u64 {
            return fail(format!(
                "utoc: chunk {i} ends in block {last}, past the {} blocks declared",
                toc.blocks.len()
            ));
        }
    }
    Ok(toc)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    const FLAG_COMPRESSED: u8 = 1;

    /// A synthetic version-5 TOC with every structure the parser reads, so a
    /// test that breaks one field breaks exactly that field.
    pub struct TocSpec {
        pub flags: u8,
        pub chunks: Vec<(ChunkId, OffsetLength)>,
        pub blocks: Vec<CompressedBlock>,
        pub methods: Vec<&'static str>,
        pub hash_size: i32,
        pub directory_index: Vec<u8>,
        pub block_size: u32,
    }

    impl Default for TocSpec {
        fn default() -> Self {
            TocSpec {
                flags: FLAG_COMPRESSED | FLAG_SIGNED | FLAG_INDEXED,
                chunks: Vec::new(),
                blocks: Vec::new(),
                methods: vec!["Oodle"],
                hash_size: 8,
                directory_index: Vec::new(),
                block_size: 0x40000,
            }
        }
    }

    pub fn build_toc(spec: &TocSpec) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(TOC_MAGIC);
        out.extend_from_slice(&[SUPPORTED_TOC_VERSION, 0, 0, 0]);
        for v in [
            TOC_HEADER_SIZE,
            spec.chunks.len() as u32,
            spec.blocks.len() as u32,
            COMPRESSED_BLOCK_ENTRY_SIZE,
            spec.methods.len() as u32,
            32,
            spec.block_size,
            spec.directory_index.len() as u32,
            1,
        ] {
            out.extend_from_slice(&v.to_le_bytes());
        }
        out.extend_from_slice(&0x1122_3344_5566_7788u64.to_le_bytes());
        out.extend_from_slice(&[0u8; 16]);
        out.extend_from_slice(&[spec.flags, 0, 0, 0]);
        out.extend_from_slice(&1u32.to_le_bytes());
        out.extend_from_slice(&u64::MAX.to_le_bytes());
        out.extend_from_slice(&0u32.to_le_bytes());
        out.extend_from_slice(&[0u8; 4 + 40]);
        assert_eq!(out.len(), TOC_HEADER_SIZE as usize);
        for (id, _) in &spec.chunks {
            out.extend_from_slice(&id.id.to_le_bytes());
            out.extend_from_slice(&[0, 0, 0]); // chunk index, padding
            out.push(id.chunk_type);
        }
        for (_, ol) in &spec.chunks {
            out.extend_from_slice(&ol.offset.to_be_bytes()[3..]);
            out.extend_from_slice(&ol.length.to_be_bytes()[3..]);
        }
        out.extend_from_slice(&0u32.to_le_bytes()); // one perfect-hash seed
        for b in &spec.blocks {
            out.extend_from_slice(&b.offset.to_le_bytes()[..5]);
            out.extend_from_slice(&b.compressed_size.to_le_bytes()[..3]);
            out.extend_from_slice(&b.uncompressed_size.to_le_bytes()[..3]);
            out.push(b.method);
        }
        for m in &spec.methods {
            let mut name = [0u8; 32];
            name[..m.len()].copy_from_slice(m.as_bytes());
            out.extend_from_slice(&name);
        }
        if spec.flags & FLAG_SIGNED != 0 {
            out.extend_from_slice(&spec.hash_size.to_le_bytes());
            out.extend(std::iter::repeat_n(
                0xAAu8,
                spec.hash_size.max(0) as usize * 2,
            ));
            out.extend(std::iter::repeat_n(0xBBu8, spec.blocks.len() * 20));
        }
        out.extend_from_slice(&spec.directory_index);
        out.extend(std::iter::repeat_n(0xCCu8, spec.chunks.len() * META_SIZE));
        out
    }

    /// An indexed, unsigned TOC with one chunk (id 1) at `(offset, length)`
    /// over `blocks` of `(offset, compressed size, uncompressed size, method)`.
    pub fn one_chunk_toc(
        chunk_type: u8,
        (offset, length): (u64, u64),
        block_size: u32,
        methods: Vec<&'static str>,
        blocks: &[(u64, u32, u32, u8)],
    ) -> TocSpec {
        let block = |&(offset, compressed_size, uncompressed_size, method)| CompressedBlock {
            offset,
            compressed_size,
            uncompressed_size,
            method,
        };
        TocSpec {
            flags: FLAG_INDEXED,
            block_size,
            methods,
            chunks: vec![(
                ChunkId { id: 1, chunk_type },
                OffsetLength { offset, length },
            )],
            blocks: blocks.iter().map(block).collect(),
            ..TocSpec::default()
        }
    }

    fn two_chunk_spec() -> TocSpec {
        TocSpec {
            chunks: vec![
                (
                    ChunkId {
                        id: 0xDEAD_BEEF,
                        chunk_type: CHUNK_EXPORT_BUNDLE_DATA,
                    },
                    OffsetLength {
                        offset: 0,
                        length: 100,
                    },
                ),
                (
                    ChunkId {
                        id: 7,
                        chunk_type: 6,
                    },
                    OffsetLength {
                        offset: 0x40000,
                        length: 68,
                    },
                ),
            ],
            blocks: vec![
                CompressedBlock {
                    offset: 0,
                    compressed_size: 50,
                    uncompressed_size: 100,
                    method: 1,
                },
                CompressedBlock {
                    offset: 64,
                    compressed_size: 68,
                    uncompressed_size: 68,
                    method: 0,
                },
            ],
            directory_index: vec![1, 2, 3, 4, 5],
            ..TocSpec::default()
        }
    }

    #[test]
    fn a_synthetic_toc_parses_field_for_field() {
        let spec = two_chunk_spec();
        let toc = parse_toc(&build_toc(&spec)).unwrap();
        assert_eq!(toc.container_id, 0x1122_3344_5566_7788);
        assert_eq!(toc.chunk_ids.len(), 2);
        assert_eq!((toc.chunk_ids[1].id, toc.chunk_ids[1].chunk_type), (7, 6));
        assert_eq!(toc.chunk_ids[0].chunk_type, CHUNK_EXPORT_BUNDLE_DATA);
        assert_eq!(toc.chunks[1].offset, 0x40000);
        assert_eq!(toc.blocks, spec.blocks);
        assert_eq!(toc.methods, ["Oodle"]);
        assert_eq!(toc.directory_index, [1, 2, 3, 4, 5]);
        assert_eq!(toc.method_name(1).unwrap(), Some("Oodle"));
        assert_eq!(toc.method_name(0).unwrap(), None);
    }

    #[test]
    fn a_layout_that_does_not_end_on_the_meta_block_is_refused() {
        let mut bytes = build_toc(&two_chunk_spec());
        bytes.push(0);
        let err = parse_toc(&bytes).unwrap_err();
        assert!(err.0.contains("layout does not match"), "{err}");
        let mut short = build_toc(&two_chunk_spec());
        short.pop();
        assert!(parse_toc(&short).is_err());
    }

    #[test]
    fn a_wrong_signature_size_misplaces_the_index_and_is_caught() {
        let spec = two_chunk_spec();
        let mut bytes = build_toc(&spec);
        // The signature size follows the method names; claim 4 bytes too many.
        let at = bytes.len()
            - spec.chunks.len() * META_SIZE
            - spec.directory_index.len()
            - spec.blocks.len() * 20
            - spec.hash_size as usize * 2
            - 4;
        bytes[at] += 2;
        assert!(parse_toc(&bytes).is_err());
    }

    #[test]
    fn other_versions_and_encryption_are_refused_by_name() {
        let mut bytes = build_toc(&two_chunk_spec());
        bytes[16] = 8;
        assert!(parse_toc(&bytes).unwrap_err().0.contains("version 8"));

        let spec = TocSpec {
            flags: FLAG_COMPRESSED | FLAG_ENCRYPTED | FLAG_SIGNED | FLAG_INDEXED,
            ..two_chunk_spec()
        };
        assert!(
            parse_toc(&build_toc(&spec))
                .unwrap_err()
                .0
                .contains("encrypted")
        );

        let mut bad = build_toc(&two_chunk_spec());
        bad[0] = b'x';
        assert!(parse_toc(&bad).is_err());
    }

    #[test]
    fn a_chunk_past_the_last_block_is_refused() {
        let mut spec = two_chunk_spec();
        spec.chunks[1].1.offset = 0x80000;
        assert!(
            parse_toc(&build_toc(&spec))
                .unwrap_err()
                .0
                .contains("past the")
        );
    }

    #[test]
    fn an_undeclared_compression_method_is_refused() {
        let mut spec = two_chunk_spec();
        spec.blocks[0].method = 2;
        assert!(parse_toc(&build_toc(&spec)).is_err());
    }
}
