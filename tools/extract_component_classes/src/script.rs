//! The script object map in `global.ucas`: native (`/Script/...`) objects by
//! `FPackageObjectIndex`. The chunk is a name batch, an `i32` count, and that
//! many 32-byte `FScriptObjectEntry` records (mapped name, own global index,
//! outer's global index, CDO class index). Paths are rebuilt by walking outers
//! and joined as UE's `GetPathName` joins them: `:` before an object whose
//! outer is a top-level object (one outered to the package), `.` everywhere
//! else (`/Script/Pkg.Object:Subobject.Inner`).

use std::collections::HashMap;

use crate::cityhash::{INDEX_MASK, hash_path};
use crate::names::{MappedName, read_name_batch};
use crate::reader::{Cursor, Result, fail};

/// `FPackageObjectIndex` kinds, from the top two bits.
pub const KIND_EXPORT: u64 = 0;
pub const KIND_SCRIPT_IMPORT: u64 = 1;
pub const KIND_PACKAGE_IMPORT: u64 = 2;
pub const KIND_NULL: u64 = 3;

pub fn index_kind(index: u64) -> u64 {
    index >> 62
}

pub fn is_null(index: u64) -> bool {
    index_kind(index) == KIND_NULL
}

#[derive(Debug, Clone)]
struct ScriptObject {
    name: String,
    outer: u64,
}

#[derive(Debug, Clone)]
pub struct ScriptObjects {
    objects: HashMap<u64, ScriptObject>,
}

/// What the self-check found; printed on every run.
#[derive(Debug, Clone, Copy, Default)]
pub struct ScriptCheck {
    pub objects: usize,
    pub paths_resolved: usize,
    pub hash_matches: usize,
    pub hash_mismatches: usize,
}

pub fn parse_script_objects(bytes: &[u8]) -> Result<ScriptObjects> {
    let mut c = Cursor::new(bytes, "script objects");
    let names = read_name_batch(&mut c)?;
    let n = c.count(32)?;
    let mut objects = HashMap::with_capacity(n);
    for i in 0..n {
        let name = MappedName::read(&mut c)?;
        let global = c.u64()?;
        let outer = c.u64()?;
        let _cdo_class = c.u64()?;
        let rendered = name.render(&names)?;
        if index_kind(global) != KIND_SCRIPT_IMPORT {
            return fail(format!(
                "script objects: entry {i} ({rendered}) has global index {global:#x}, not a script import"
            ));
        }
        if objects
            .insert(
                global,
                ScriptObject {
                    name: rendered,
                    outer,
                },
            )
            .is_some()
        {
            return fail(format!(
                "script objects: global index {global:#x} appears twice"
            ));
        }
    }
    if c.remaining() != 0 {
        return fail(format!(
            "script objects: {} bytes left after {n} entries",
            c.remaining()
        ));
    }
    Ok(ScriptObjects { objects })
}

impl ScriptObjects {
    /// The full path of a script object, `None` if the index is unknown or its
    /// outer chain breaks.
    pub fn path_of(&self, index: u64) -> Option<String> {
        let mut chain = Vec::new();
        let mut at = index;
        while !is_null(at) {
            if chain.len() > 64 {
                return None;
            }
            let obj = self.objects.get(&at)?;
            chain.push(obj);
            at = obj.outer;
        }
        let mut path = String::new();
        for (depth, obj) in chain.iter().rev().enumerate() {
            match depth {
                0 => {}
                2 => path.push(':'),
                _ => path.push('.'),
            }
            path.push_str(&obj.name);
        }
        Some(path)
    }

    /// Rebuild every path and hash it as the engine does; the result must be the
    /// object's own global index. This proves the names and the outer walk, not
    /// the separators, which the hash folds alike into `/`.
    pub fn verify(&self) -> ScriptCheck {
        let mut check = ScriptCheck {
            objects: self.objects.len(),
            ..ScriptCheck::default()
        };
        for &index in self.objects.keys() {
            let Some(path) = self.path_of(index) else {
                continue;
            };
            check.paths_resolved += 1;
            if hash_path(&path) == index & INDEX_MASK {
                check.hash_matches += 1;
            } else {
                check.hash_mismatches += 1;
            }
        }
        check
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::names::tests::name_batch;

    pub fn script_index(path: &str) -> u64 {
        (KIND_SCRIPT_IMPORT << 62) | hash_path(path)
    }

    /// A script object chunk for `(name index, number, path, outer path)`
    /// entries; an outer of `None` is null, as a package's is.
    pub fn build_script_objects(
        names: &[&str],
        objects: &[(u32, u32, &str, Option<&str>)],
    ) -> Vec<u8> {
        let mut out = name_batch(names);
        out.extend_from_slice(&(objects.len() as i32).to_le_bytes());
        for (name, number, path, outer) in objects {
            out.extend_from_slice(&((2u32 << 30) | name).to_le_bytes());
            out.extend_from_slice(&number.to_le_bytes());
            out.extend_from_slice(&script_index(path).to_le_bytes());
            let outer = outer.map_or(u64::MAX, script_index);
            out.extend_from_slice(&outer.to_le_bytes());
            out.extend_from_slice(&u64::MAX.to_le_bytes());
        }
        out
    }

    /// A script object chunk for `paths`, each named by the text after its
    /// last `.` or `:` and outered to the text before it.
    pub fn script_from_paths(paths: &[&str]) -> Vec<u8> {
        let (mut names, mut objects) = (Vec::new(), Vec::new());
        for (i, path) in paths.iter().enumerate() {
            let (outer, leaf) = match path.rsplit_once(['.', ':']) {
                Some((outer, leaf)) => (Some(outer), leaf),
                None => (None, *path),
            };
            names.push(leaf);
            objects.push((i as u32, 0, *path, outer));
        }
        build_script_objects(&names, &objects)
    }

    fn sample() -> Vec<u8> {
        script_from_paths(&[
            "/Script/ShooterGame",
            "/Script/ShooterGame.AresInventory",
            "/Script/ShooterGame.AresInventory:Inner",
            "/Script/ShooterGame.AresInventory:Inner.Deeper",
        ])
    }

    /// Depth 3 is where the rule shows: below the first subobject UE joins
    /// with `.` again.
    #[test]
    fn paths_use_a_colon_only_below_a_top_level_object() {
        let objs = parse_script_objects(&sample()).unwrap();
        assert_eq!(
            objs.path_of(script_index("/Script/ShooterGame.AresInventory"))
                .as_deref(),
            Some("/Script/ShooterGame.AresInventory")
        );
        assert_eq!(
            objs.path_of(script_index("/Script/ShooterGame.AresInventory:Inner"))
                .as_deref(),
            Some("/Script/ShooterGame.AresInventory:Inner")
        );
        assert_eq!(
            objs.path_of(script_index(
                "/Script/ShooterGame.AresInventory:Inner.Deeper"
            ))
            .as_deref(),
            Some("/Script/ShooterGame.AresInventory:Inner.Deeper")
        );
        assert_eq!(objs.path_of(script_index("/Script/Nope.Missing")), None);
        let check = objs.verify();
        assert_eq!(check.objects, 4);
        assert_eq!(check.hash_matches, 4);
        assert_eq!(check.hash_mismatches, 0);
    }

    /// The hash check catches a renamed object; a mis-separated path would
    /// still match, since `.` and `:` both hash as `/`.
    #[test]
    fn a_name_that_does_not_hash_to_its_index_is_counted() {
        let bytes = build_script_objects(
            &["/Script/ShooterGame", "Renamed"],
            &[
                (0, 0, "/Script/ShooterGame", None),
                (
                    1,
                    0,
                    "/Script/ShooterGame.Original",
                    Some("/Script/ShooterGame"),
                ),
            ],
        );
        let check = parse_script_objects(&bytes).unwrap().verify();
        assert_eq!(check.hash_matches, 1);
        assert_eq!(check.hash_mismatches, 1);
    }

    #[test]
    fn trailing_bytes_and_duplicates_are_errors() {
        let mut bytes = sample();
        bytes.push(0);
        assert!(parse_script_objects(&bytes).is_err());

        let dup = script_from_paths(&["/Script/A", "/Script/A"]);
        assert!(parse_script_objects(&dup).unwrap_err().0.contains("twice"));
    }
}
