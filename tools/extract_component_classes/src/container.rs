//! One IoStore container: its `.utoc` parsed, its `.ucas` read on demand.
//! Read-only: `File::open` asks for read access and, on Windows, shares read,
//! write and delete, so the game or its patcher is never locked out. Reading
//! part of a chunk decompresses only the blocks it touches, which keeps a scan
//! of every package header to a fraction of the 30 GB of `.ucas`.

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

use crate::reader::{Error, Result, fail};
use crate::toc::{Toc, parse_toc};

pub struct Container {
    /// File stem, e.g. `pakchunk0-WindowsClient`.
    pub name: String,
    pub ucas_path: PathBuf,
    pub toc: Toc,
}

impl Container {
    pub fn open(utoc_path: &Path) -> Result<Container> {
        let bytes =
            std::fs::read(utoc_path).map_err(|e| Error(format!("{}: {e}", utoc_path.display())))?;
        let toc = parse_toc(&bytes).map_err(|e| Error(format!("{}: {e}", utoc_path.display())))?;
        let name = utoc_path
            .file_stem()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_default();
        Ok(Container {
            name,
            ucas_path: utoc_path.with_extension("ucas"),
            toc,
        })
    }

    pub fn open_ucas(&self) -> Result<File> {
        File::open(&self.ucas_path).map_err(|e| Error(format!("{}: {e}", self.ucas_path.display())))
    }

    /// The first `limit` bytes of chunk `entry`, or all of a shorter chunk.
    pub fn read_chunk(
        &self,
        ucas: &mut (impl Read + Seek),
        entry: usize,
        limit: u64,
    ) -> Result<Vec<u8>> {
        let Some(chunk) = self.toc.chunks.get(entry) else {
            return fail(format!("{}: chunk {entry} out of range", self.name));
        };
        let want = chunk.length.min(limit);
        if want == 0 {
            return Ok(Vec::new());
        }
        let block_size = u64::from(self.toc.block_size);
        let first = chunk.offset / block_size;
        let last = (chunk.offset + want - 1) / block_size;
        let mut out = Vec::with_capacity(((last - first + 1) * block_size) as usize);
        for index in first..=last {
            self.read_block(ucas, index as usize, &mut out)?;
        }
        let start = (chunk.offset - first * block_size) as usize;
        let end = start + want as usize;
        if end > out.len() {
            return fail(format!(
                "{}: chunk {entry} needs {end} bytes of its blocks, they hold {}",
                self.name,
                out.len()
            ));
        }
        out.truncate(end);
        out.drain(..start);
        Ok(out)
    }

    fn read_block(
        &self,
        ucas: &mut (impl Read + Seek),
        index: usize,
        out: &mut Vec<u8>,
    ) -> Result<()> {
        let Some(block) = self.toc.blocks.get(index) else {
            return fail(format!("{}: block {index} out of range", self.name));
        };
        let mut raw = vec![0u8; block.compressed_size as usize];
        ucas.seek(SeekFrom::Start(block.offset))
            .and_then(|_| ucas.read_exact(&mut raw))
            .map_err(|e| {
                Error(format!(
                    "{}: block {index} at {}: {e}",
                    self.name, block.offset
                ))
            })?;
        match self.toc.method_name(block.method)? {
            None => {
                if block.compressed_size != block.uncompressed_size {
                    return fail(format!(
                        "{}: stored block {index} is {} bytes but declares {} uncompressed",
                        self.name, block.compressed_size, block.uncompressed_size
                    ));
                }
                out.extend_from_slice(&raw);
            }
            Some("Oodle") => {
                let size = block.uncompressed_size as usize;
                let start = out.len();
                out.resize(start + size, 0);
                // A fresh extractor per block (it keeps decoder state across
                // calls), and `read` over a slice so unread input shows.
                let mut unread: &[u8] = &raw;
                let n = oozextract::Extractor::new()
                    .read(&mut unread, &mut out[start..])
                    .map_err(|e| Error(format!("{}: block {index}: Oodle: {e:?}", self.name)))?;
                if n != size {
                    return fail(format!(
                        "{}: block {index} decompressed to {n} bytes, declares {size}",
                        self.name
                    ));
                }
                if !unread.is_empty() {
                    return fail(format!(
                        "{}: block {index}: Oodle left {} of {} compressed bytes unread",
                        self.name,
                        unread.len(),
                        raw.len()
                    ));
                }
            }
            Some(other) => {
                return fail(format!(
                    "{}: block {index} uses compression method {other:?}, which this tool does not read",
                    self.name
                ));
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::toc::tests::{TocSpec, build_toc, one_chunk_toc};
    use std::io::Cursor as IoCursor;

    fn container(spec: &TocSpec) -> Container {
        let toc = parse_toc(&build_toc(spec)).unwrap();
        Container {
            name: "t".to_owned(),
            ucas_path: PathBuf::new(),
            toc,
        }
    }

    /// Stored (uncompressed) 8-byte blocks, chunk 0 at stream offset 4 spanning
    /// three of them, so a read starts mid-block and crosses two boundaries.
    fn stored() -> (Container, Vec<u8>) {
        let blocks = [(0, 8, 8, 0), (8, 8, 8, 0), (16, 8, 8, 0), (24, 8, 8, 0)];
        let spec = one_chunk_toc(1, (4, 18), 8, vec![], &blocks);
        (container(&spec), (0u8..32).collect())
    }

    #[test]
    fn a_chunk_is_cut_out_of_the_blocks_it_spans() {
        let (c, ucas) = stored();
        let mut f = IoCursor::new(ucas);
        assert_eq!(
            c.read_chunk(&mut f, 0, u64::MAX).unwrap(),
            (4u8..22).collect::<Vec<_>>()
        );
        assert_eq!(c.read_chunk(&mut f, 0, 3).unwrap(), [4, 5, 6]);
    }

    #[test]
    fn a_short_ucas_is_an_error() {
        let (c, mut ucas) = stored();
        ucas.truncate(20);
        let mut f = IoCursor::new(ucas);
        assert!(c.read_chunk(&mut f, 0, u64::MAX).is_err());
    }

    #[test]
    fn a_stored_block_whose_sizes_disagree_is_an_error() {
        let (mut c, ucas) = stored();
        c.toc.blocks[1].uncompressed_size = 9;
        let mut f = IoCursor::new(ucas);
        assert!(c.read_chunk(&mut f, 0, u64::MAX).is_err());
    }

    /// One Oodle block: an eight-byte uncompressed Kraken block (header
    /// `0x4C 0x06`), then `unread` bytes inside the compressed size the codec
    /// never reaches.
    fn oodle(unread: usize) -> (Container, Vec<u8>) {
        let mut ucas = vec![0x4C, 0x06];
        ucas.extend(0u8..8);
        ucas.extend(std::iter::repeat_n(0xAB, unread));
        let spec = one_chunk_toc(1, (0, 8), 8, vec!["Oodle"], &[(0, ucas.len() as u32, 8, 1)]);
        (container(&spec), ucas)
    }

    #[test]
    fn an_oodle_block_the_codec_does_not_read_to_the_end_is_an_error() {
        let (c, ucas) = oodle(0);
        let mut f = IoCursor::new(ucas);
        assert_eq!(
            c.read_chunk(&mut f, 0, u64::MAX).unwrap(),
            (0u8..8).collect::<Vec<_>>()
        );

        let (c, ucas) = oodle(5);
        let mut f = IoCursor::new(ucas);
        let err = c.read_chunk(&mut f, 0, u64::MAX).unwrap_err();
        assert!(err.0.contains("5 of 15"), "{err}");
    }
}
