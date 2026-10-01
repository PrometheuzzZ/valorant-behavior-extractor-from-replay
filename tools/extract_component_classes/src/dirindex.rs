//! The directory index inside a `.utoc`, which names the chunks: a mount point,
//! then directory, file and string tables. Directories and files are singly
//! linked by index (first child / next sibling, first file / next file), a
//! file's user data is its TOC entry, and `u32::MAX` ends a list or means no name.

use crate::reader::{Cursor, Result, fail};

const NONE: u32 = u32::MAX;

#[derive(Debug, Clone, Copy)]
struct DirEntry {
    name: u32,
    first_child: u32,
    next_sibling: u32,
    first_file: u32,
}

#[derive(Debug, Clone, Copy)]
struct FileEntry {
    name: u32,
    next_file: u32,
    user_data: u32,
}

#[derive(Debug, Clone)]
pub struct DirectoryIndex {
    mount_point: String,
    dirs: Vec<DirEntry>,
    files: Vec<FileEntry>,
    strings: Vec<String>,
}

pub fn parse_directory_index(bytes: &[u8]) -> Result<DirectoryIndex> {
    let mut c = Cursor::new(bytes, "directory index");
    let mount_point = c.fstring()?;
    let nd = c.count(16)?;
    let mut dirs = Vec::with_capacity(nd);
    for _ in 0..nd {
        dirs.push(DirEntry {
            name: c.u32()?,
            first_child: c.u32()?,
            next_sibling: c.u32()?,
            first_file: c.u32()?,
        });
    }
    let nf = c.count(12)?;
    let mut files = Vec::with_capacity(nf);
    for _ in 0..nf {
        files.push(FileEntry {
            name: c.u32()?,
            next_file: c.u32()?,
            user_data: c.u32()?,
        });
    }
    let ns = c.count(4)?;
    let mut strings = Vec::with_capacity(ns);
    for _ in 0..ns {
        strings.push(c.fstring()?);
    }
    if c.remaining() != 0 {
        return fail(format!(
            "directory index: {} bytes left over after the string table",
            c.remaining()
        ));
    }
    Ok(DirectoryIndex {
        mount_point,
        dirs,
        files,
        strings,
    })
}

impl DirectoryIndex {
    fn name(&self, index: u32) -> Result<&str> {
        match self.strings.get(index as usize) {
            Some(s) => Ok(s.as_str()),
            None => fail(format!(
                "directory index: name {index} out of range ({} strings)",
                self.strings.len()
            )),
        }
    }

    /// Every file as `(mount point + path, TOC entry index)`, in index order.
    ///
    /// A link that points outside its table, or a walk that visits more
    /// entries than exist (a cycle), is an error: the listing would otherwise
    /// be silently short or endless.
    pub fn files(&self) -> Result<Vec<(String, u32)>> {
        let mut out = Vec::with_capacity(self.files.len());
        if self.dirs.is_empty() {
            return Ok(out);
        }
        let mut dirs_seen = 0usize;
        let mut stack: Vec<(u32, String)> = vec![(0, self.mount_point.clone())];
        while let Some((d, prefix)) = stack.pop() {
            dirs_seen += 1;
            if dirs_seen > self.dirs.len() {
                return fail("directory index: directory links form a cycle");
            }
            let dir = match self.dirs.get(d as usize) {
                Some(e) => *e,
                None => return fail(format!("directory index: directory {d} out of range")),
            };
            let mut f = dir.first_file;
            while f != NONE {
                if out.len() >= self.files.len() {
                    return fail("directory index: file links form a cycle");
                }
                let file = match self.files.get(f as usize) {
                    Some(e) => *e,
                    None => return fail(format!("directory index: file {f} out of range")),
                };
                out.push((format!("{prefix}{}", self.name(file.name)?), file.user_data));
                f = file.next_file;
            }
            // Children are pushed in reverse so they pop in list order.
            let mut children = Vec::new();
            let mut child = dir.first_child;
            while child != NONE {
                if children.len() >= self.dirs.len() {
                    return fail("directory index: sibling links form a cycle");
                }
                let entry = match self.dirs.get(child as usize) {
                    Some(e) => *e,
                    None => {
                        return fail(format!("directory index: directory {child} out of range"));
                    }
                };
                let name = if entry.name == NONE {
                    return fail(format!(
                        "directory index: child directory {child} has no name"
                    ));
                } else {
                    self.name(entry.name)?
                };
                children.push((child, format!("{prefix}{name}/")));
                child = entry.next_sibling;
            }
            stack.extend(children.into_iter().rev());
        }
        if out.len() != self.files.len() {
            return fail(format!(
                "directory index: walk reached {} of {} files",
                out.len(),
                self.files.len()
            ));
        }
        Ok(out)
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    fn fstring(out: &mut Vec<u8>, s: &str) {
        out.extend_from_slice(&((s.len() + 1) as i32).to_le_bytes());
        out.extend_from_slice(s.as_bytes());
        out.push(0);
    }

    /// Serialize a directory index. `dirs` are `(name, first_child,
    /// next_sibling, first_file)` and `files` are `(name, next_file,
    /// user_data)`, both as raw indices.
    pub fn build_index(
        mount: &str,
        dirs: &[(u32, u32, u32, u32)],
        files: &[(u32, u32, u32)],
        strings: &[&str],
    ) -> Vec<u8> {
        let mut out = Vec::new();
        fstring(&mut out, mount);
        out.extend_from_slice(&(dirs.len() as i32).to_le_bytes());
        for d in dirs {
            for v in [d.0, d.1, d.2, d.3] {
                out.extend_from_slice(&v.to_le_bytes());
            }
        }
        out.extend_from_slice(&(files.len() as i32).to_le_bytes());
        for f in files {
            for v in [f.0, f.1, f.2] {
                out.extend_from_slice(&v.to_le_bytes());
            }
        }
        out.extend_from_slice(&(strings.len() as i32).to_le_bytes());
        for s in strings {
            fstring(&mut out, s);
        }
        out
    }

    /// root/ holds a.uasset; root/Sub/ holds b.uasset and c.umap.
    fn sample() -> Vec<u8> {
        build_index(
            "../../../Game/",
            &[(NONE, 1, NONE, 0), (0, NONE, NONE, 1)],
            &[(1, NONE, 7), (2, 2, 8), (3, NONE, 9)],
            &["Sub", "a.uasset", "b.uasset", "c.umap"],
        )
    }

    #[test]
    fn the_walk_yields_every_file_with_its_toc_entry() {
        let idx = parse_directory_index(&sample()).unwrap();
        assert_eq!(
            idx.files().unwrap(),
            [
                ("../../../Game/a.uasset".to_owned(), 7),
                ("../../../Game/Sub/b.uasset".to_owned(), 8),
                ("../../../Game/Sub/c.umap".to_owned(), 9),
            ]
        );
    }

    #[test]
    fn trailing_bytes_are_an_error() {
        let mut bytes = sample();
        bytes.push(0);
        assert!(parse_directory_index(&bytes).is_err());
    }

    #[test]
    fn a_file_cycle_is_an_error_not_an_endless_walk() {
        let bytes = build_index(
            "/",
            &[(NONE, NONE, NONE, 0)],
            &[(0, 1, 0), (0, 0, 1)],
            &["x"],
        );
        let idx = parse_directory_index(&bytes).unwrap();
        assert!(idx.files().is_err());
    }

    #[test]
    fn an_out_of_range_link_is_an_error() {
        let bytes = build_index("/", &[(NONE, NONE, NONE, 5)], &[(0, NONE, 0)], &["x"]);
        let idx = parse_directory_index(&bytes).unwrap();
        assert!(idx.files().is_err());
    }

    #[test]
    fn an_unreachable_file_is_reported() {
        // Two files, but the directory only links the first.
        let bytes = build_index(
            "/",
            &[(NONE, NONE, NONE, 0)],
            &[(0, NONE, 0), (0, NONE, 1)],
            &["x"],
        );
        let idx = parse_directory_index(&bytes).unwrap();
        assert!(idx.files().unwrap_err().0.contains("1 of 2"));
    }
}
