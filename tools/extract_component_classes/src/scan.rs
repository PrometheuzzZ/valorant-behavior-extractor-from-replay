//! Walk every package and pick out component templates in the two export
//! shapes of docs/DATA.md ("Reading component classes out of the game", steps
//! 3-4): `gen_variable`, a `<Name>_GEN_VARIABLE` template spawned as `<Name>`,
//! the bare string the replay sends (child Blueprints' overrides reuse name and
//! class); and `cdo_subobject`, a C++-created component, a subobject of
//! `Default__<Class>` under its instance name and the only shape five 13.06
//! components have. A class resolves through `global.ucas`, or to another
//! package's class and up its super chain to the first native one; anything
//! unresolvable is `?`, never a guess.

use std::collections::HashMap;

use crate::cityhash::{INDEX_MASK, hash_package_name};
use crate::container::Container;
use crate::reader::{Result, fail};
use crate::script::{
    KIND_EXPORT, KIND_NULL, KIND_PACKAGE_IMPORT, KIND_SCRIPT_IMPORT, ScriptObjects, index_kind,
    is_null,
};
use crate::toc::CHUNK_EXPORT_BUNDLE_DATA;
use crate::zen::{ExportEntry, PackageHeader, declared_header_size, parse_package_header};

pub const GEN_VARIABLE_SUFFIX: &str = "_GEN_VARIABLE";
pub const CDO_PREFIX: &str = "Default__";

/// One package to read; `file` is the directory index's name for it, if any.
#[derive(Debug, Clone)]
pub struct Job {
    pub container: usize,
    pub entry: usize,
    pub file: Option<String>,
}

/// Where an export's class index points, before the cross-package pass.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ClassRef {
    Script(u64),
    /// `(package id, package name as imported, public export hash)`.
    Package(u64, String, u64),
    /// Another export of the same package: `(package id, its public export
    /// hash, its name)`. A hash of 0 means the export is not public and cannot
    /// be looked up.
    Local(u64, u64, String),
    Null,
    /// The index could not be decoded against this package's tables.
    Bad(String),
}

#[derive(Debug, Clone)]
pub struct Candidate {
    pub kind: &'static str,
    pub instance: String,
    pub export: String,
    pub outer: String,
    pub class: ClassRef,
    /// A `_GEN_VARIABLE` export whose FName carries an instance number. The
    /// instance takes the number too (`X_GEN_VARIABLE` number 3 is listed as
    /// `X_2`), though the name that would spawn from it is not established.
    /// Counted in `gen_variable_numbered`; no output column flags the row.
    pub numbered: bool,
}

/// A class defined by some package (see [`ClassTable`] for its key).
#[derive(Debug, Clone)]
pub struct ClassExport {
    pub path: String,
    pub super_ref: ClassRef,
}

#[derive(Debug, Clone)]
pub struct PackageScan {
    pub name: String,
    pub package_id_matches: bool,
    /// `None` when the directory index does not name this chunk.
    pub file_stem_matches: Option<bool>,
    pub exports: usize,
    pub candidates: Vec<Candidate>,
    pub classes: Vec<(u64, ClassExport)>,
}

pub fn class_ref(pkg: &PackageHeader, package_id: u64, index: u64) -> ClassRef {
    match index_kind(index) {
        KIND_NULL => ClassRef::Null,
        KIND_SCRIPT_IMPORT => ClassRef::Script(index),
        KIND_PACKAGE_IMPORT => {
            let raw = index & INDEX_MASK;
            let pkg_index = (raw >> 32) as usize;
            let hash_index = (raw & 0xffff_ffff) as usize;
            let (Some(name), Some(&hash)) = (
                pkg.imported_package_names.get(pkg_index),
                pkg.imported_public_export_hashes.get(hash_index),
            ) else {
                return ClassRef::Bad(format!(
                    "package import {pkg_index}/{hash_index} outside {} packages / {} hashes",
                    pkg.imported_package_names.len(),
                    pkg.imported_public_export_hashes.len()
                ));
            };
            ClassRef::Package(hash_package_name(name), name.clone(), hash)
        }
        KIND_EXPORT => match pkg.exports.get(index as usize) {
            Some(e) => ClassRef::Local(package_id, e.public_export_hash, pkg.export_name(e)),
            None => ClassRef::Bad(format!(
                "local export {index} outside {} exports",
                pkg.exports.len()
            )),
        },
        _ => unreachable!("two bits have four values"),
    }
}

/// A class object: its own class is native and named `*Class`, and it has a
/// super (the export map sets one only on structs). Neither suffices: in 13.06
/// the name alone also admits 257 objects with no super (mostly
/// `MVVMViewClass`), the super alone 21,059 functions and script structs; the
/// 73,057 passing both are exactly the exports a class default object in their
/// package instantiates.
fn is_class_object(script: &ScriptObjects, export: &ExportEntry) -> bool {
    if index_kind(export.class_index) != KIND_SCRIPT_IMPORT || is_null(export.super_index) {
        return false;
    }
    script.path_of(export.class_index).is_some_and(|p| {
        p.rsplit(['.', ':'])
            .next()
            .is_some_and(|leaf| leaf.ends_with("Class"))
    })
}

pub fn scan_header(
    pkg: &PackageHeader,
    package_id: u64,
    script: &ScriptObjects,
) -> (Vec<Candidate>, Vec<(u64, ClassExport)>) {
    let mut candidates = Vec::new();
    let mut classes = Vec::new();
    for export in &pkg.exports {
        let base = export
            .object_name
            .base(&pkg.names)
            .unwrap_or("?")
            .to_owned();
        let rendered = pkg.export_name(export);
        let outer = if index_kind(export.outer_index) == KIND_EXPORT {
            pkg.exports.get(export.outer_index as usize)
        } else {
            None
        };
        let outer_name = outer.map_or_else(String::new, |o| pkg.export_name(o));

        if is_class_object(script, export) && export.public_export_hash != 0 {
            let path = match outer {
                None => format!("{}.{rendered}", pkg.name),
                Some(_) => format!("{}.{outer_name}:{rendered}", pkg.name),
            };
            classes.push((
                export.public_export_hash,
                ClassExport {
                    path,
                    super_ref: class_ref(pkg, package_id, export.super_index),
                },
            ));
        }

        if let Some(stem) = base.strip_suffix(GEN_VARIABLE_SUFFIX) {
            let numbered = export.object_name.number != 0;
            let instance = if numbered {
                format!("{stem}_{}", export.object_name.number - 1)
            } else {
                stem.to_owned()
            };
            candidates.push(Candidate {
                kind: "gen_variable",
                instance,
                export: rendered,
                outer: outer_name,
                class: class_ref(pkg, package_id, export.class_index),
                numbered,
            });
            continue;
        }
        let is_cdo_child = outer.is_some_and(|o| {
            o.object_name
                .base(&pkg.names)
                .unwrap_or("")
                .starts_with(CDO_PREFIX)
        });
        if is_cdo_child {
            candidates.push(Candidate {
                kind: "cdo_subobject",
                instance: rendered.clone(),
                export: rendered,
                outer: outer_name,
                class: class_ref(pkg, package_id, export.class_index),
                numbered: false,
            });
        }
    }
    (candidates, classes)
}

pub fn scan_package(
    container: &Container,
    ucas: &mut std::fs::File,
    job: &Job,
    script: &ScriptObjects,
) -> Result<PackageScan> {
    let toc = &container.toc;
    let Some(id) = toc.chunk_ids.get(job.entry) else {
        return fail(format!("chunk {} out of range", job.entry));
    };
    if id.chunk_type != CHUNK_EXPORT_BUNDLE_DATA {
        return fail(format!(
            "chunk {} is type {}, not a package",
            job.entry, id.chunk_type
        ));
    }
    let chunk = toc.chunks[job.entry];
    let block_size = u64::from(toc.block_size);
    let first_block = block_size - chunk.offset % block_size;
    let mut data = container.read_chunk(ucas, job.entry, first_block)?;
    let header_size = u64::from(declared_header_size(&data)?);
    if header_size > data.len() as u64 {
        if header_size > chunk.length {
            return fail(format!(
                "header size {header_size} exceeds the {}-byte chunk",
                chunk.length
            ));
        }
        data = container.read_chunk(ucas, job.entry, header_size)?;
    }
    let pkg = parse_package_header(&data)?;
    let package_id_matches = hash_package_name(&pkg.name) == id.id;
    let file_stem_matches = job.file.as_ref().map(|f| {
        let file = f.rsplit('/').next().unwrap_or(f);
        let stem = file.rsplit_once('.').map_or(file, |(s, _)| s);
        let leaf = pkg.name.rsplit('/').next().unwrap_or(&pkg.name);
        stem.eq_ignore_ascii_case(leaf)
    });
    let (candidates, classes) = scan_header(&pkg, id.id, script);
    Ok(PackageScan {
        name: pkg.name.clone(),
        package_id_matches,
        file_stem_matches,
        exports: pkg.exports.len(),
        candidates,
        classes,
    })
}

/// Every class object seen, keyed by `(package id, public export hash)`.
/// `scan_header` records public classes only, so no key has hash 0 and a
/// non-public `ClassRef::Local` finds nothing here.
pub type ClassTable = HashMap<(u64, u64), ClassExport>;

/// Every `Resolved::class_kind` `resolve` returns, in summary order.
pub const CLASS_KINDS: [&str; 8] = [
    "script_import",
    "script_import_unresolved",
    "package_import",
    "package_import_unresolved",
    "export",
    "export_unresolved",
    "null",
    "bad_index",
];

/// A candidate's resolved class; any part that cannot be resolved is `?`.
#[derive(Debug, Clone)]
pub struct Resolved {
    pub class: String,
    pub class_kind: &'static str,
    pub native_class: String,
    pub class_ref: String,
}

impl Resolved {
    fn unresolved(class_kind: &'static str, class_ref: String) -> Self {
        Resolved {
            class: "?".to_owned(),
            class_kind,
            native_class: "?".to_owned(),
            class_ref,
        }
    }
}

pub fn resolve(class: &ClassRef, script: &ScriptObjects, classes: &ClassTable) -> Resolved {
    // A class some package defines, under `kind` when found and
    // `unresolved_kind` when no package read defines it.
    let defined = |key, kind, unresolved_kind, class_ref| match classes.get(&key) {
        Some(found) => Resolved {
            class: found.path.clone(),
            class_kind: kind,
            native_class: native_ancestor(&found.super_ref, script, classes),
            class_ref,
        },
        None => Resolved::unresolved(unresolved_kind, class_ref),
    };
    match class {
        ClassRef::Script(index) => {
            let class_ref = format!("script:{index:#018x}");
            match script.path_of(*index) {
                Some(path) => Resolved {
                    class: path.clone(),
                    class_kind: "script_import",
                    native_class: path,
                    class_ref,
                },
                None => Resolved::unresolved("script_import_unresolved", class_ref),
            }
        }
        ClassRef::Package(id, name, hash) => defined(
            (*id, *hash),
            "package_import",
            "package_import_unresolved",
            format!("package:{name}#{hash:#018x}"),
        ),
        ClassRef::Local(id, hash, name) => defined(
            (*id, *hash),
            "export",
            "export_unresolved",
            format!("export:{name}"),
        ),
        ClassRef::Null => Resolved::unresolved("null", "null".to_owned()),
        ClassRef::Bad(why) => Resolved::unresolved("bad_index", why.clone()),
    }
}

/// Walk a Blueprint class's super chain to the first native class. `?` when a
/// link cannot be followed; the walk is bounded so a cycle ends as `?` too.
fn native_ancestor(start: &ClassRef, script: &ScriptObjects, classes: &ClassTable) -> String {
    let mut at = start.clone();
    for _ in 0..64 {
        match at {
            ClassRef::Script(index) => {
                return script.path_of(index).unwrap_or_else(|| "?".to_owned());
            }
            ClassRef::Package(id, _, hash) | ClassRef::Local(id, hash, _) => {
                match classes.get(&(id, hash)) {
                    Some(found) => at = found.super_ref.clone(),
                    None => return "?".to_owned(),
                }
            }
            ClassRef::Null | ClassRef::Bad(_) => return "?".to_owned(),
        }
    }
    "?".to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::script::parse_script_objects;
    use crate::script::tests::{script_from_paths, script_index};
    use crate::zen::tests::{PackageSpec, build_package};

    fn script() -> ScriptObjects {
        parse_script_objects(&script_from_paths(&[
            "/Script/Engine",
            "/Script/Engine.BlueprintGeneratedClass",
            "/Script/ShooterGame",
            "/Script/ShooterGame.EquippableStateMachineComponent",
            "/Script/ShooterGame.AresCharacter",
            "/Script/Engine.AnimBlueprintGeneratedClass",
            "/Script/Engine.SoundClass",
            "/Script/UMG",
            "/Script/UMG.WidgetBlueprintGeneratedClass",
            "/Script/CoreUObject",
            "/Script/CoreUObject.Function",
        ]))
        .unwrap()
    }

    /// The class objects `scan_header` records for a one-package spec.
    fn classes_of(spec: &PackageSpec<'_>) -> Vec<(u64, String)> {
        let pkg = parse_package_header(&build_package(spec)).unwrap();
        let (_, classes) = scan_header(&pkg, 1, &script());
        classes.into_iter().map(|(h, c)| (h, c.path)).collect()
    }

    /// A character Blueprint: its class (export 0), its CDO (export 1), one
    /// SCS component whose class is native (export 2), one whose class is a
    /// Blueprint in another package (export 3), a C++ default subobject on the
    /// CDO (export 4), and an unrelated export (5).
    fn character() -> Vec<u8> {
        let bpgc = script_index("/Script/Engine.BlueprintGeneratedClass");
        let esm = script_index("/Script/ShooterGame.EquippableStateMachineComponent");
        let character = script_index("/Script/ShooterGame.AresCharacter");
        // Imported package 0, imported hash 0.
        let pkg_import = KIND_PACKAGE_IMPORT << 62;
        build_package(&PackageSpec {
            names: vec![
                "/Game/Characters/BP_Agent",
                "BP_Agent_C",
                "Default__BP_Agent_C",
                "Resume_StateMachine_GEN_VARIABLE",
                "Cooldown_GEN_VARIABLE",
                "InventoryComponent",
                "SomeMaterial",
            ],
            exports: vec![
                (1, 0, u64::MAX, bpgc, character, 0x1111),
                (2, 0, u64::MAX, 0, u64::MAX, 0x2222),
                (3, 0, 0, esm, u64::MAX, 0),
                (4, 0, 0, pkg_import, u64::MAX, 0),
                (5, 0, 1, esm, u64::MAX, 0),
                (6, 0, u64::MAX, esm, u64::MAX, 0),
            ],
            imported_hashes: vec![0x3333],
            imported_packages: vec![("/Game/Abilities/Comp_Cooldown", 0)],
            ..PackageSpec::default()
        })
    }

    /// The Blueprint class the second component points at, defined in its own
    /// package with a native parent.
    fn cooldown() -> Vec<u8> {
        let bpgc = script_index("/Script/Engine.BlueprintGeneratedClass");
        let esm = script_index("/Script/ShooterGame.EquippableStateMachineComponent");
        build_package(&PackageSpec {
            names: vec!["/Game/Abilities/Comp_Cooldown", "Comp_Cooldown_C"],
            exports: vec![(1, 0, u64::MAX, bpgc, esm, 0x3333)],
            ..PackageSpec::default()
        })
    }

    /// A child Blueprint: its class's super is `cooldown()`'s class, in
    /// another package.
    fn cooldown_child() -> PackageSpec<'static> {
        let bpgc = script_index("/Script/Engine.BlueprintGeneratedClass");
        // Imported package 0, imported hash 0.
        let parent = KIND_PACKAGE_IMPORT << 62;
        PackageSpec {
            names: vec![
                "/Game/Abilities/Comp_Cooldown_Child",
                "Comp_Cooldown_Child_C",
            ],
            exports: vec![(1, 0, u64::MAX, bpgc, parent, 0x5555)],
            imported_hashes: vec![0x3333],
            imported_packages: vec![("/Game/Abilities/Comp_Cooldown", 0)],
            ..PackageSpec::default()
        }
    }

    #[test]
    fn components_are_found_by_both_shapes_and_resolved() {
        let script = script();
        let agent = parse_package_header(&character()).unwrap();
        let agent_id = hash_package_name(&agent.name);
        let (cands, agent_classes) = scan_header(&agent, agent_id, &script);
        let cd = parse_package_header(&cooldown()).unwrap();
        let cd_id = hash_package_name(&cd.name);
        let (none, cd_classes) = scan_header(&cd, cd_id, &script);
        assert!(none.is_empty());

        let mut table = ClassTable::new();
        for (id, list) in [(agent_id, agent_classes), (cd_id, cd_classes)] {
            for (hash, class) in list {
                table.insert((id, hash), class);
            }
        }
        let got: Vec<(&str, String, Resolved)> = cands
            .iter()
            .map(|c| {
                (
                    c.kind,
                    c.instance.clone(),
                    resolve(&c.class, &script, &table),
                )
            })
            .collect();
        assert_eq!(got.len(), 3, "{got:?}");
        assert_eq!(got[0].0, "gen_variable");
        assert_eq!(got[0].1, "Resume_StateMachine");
        assert_eq!(
            got[0].2.class,
            "/Script/ShooterGame.EquippableStateMachineComponent"
        );
        assert_eq!(got[0].2.class_kind, "script_import");

        assert_eq!(got[1].1, "Cooldown");
        assert_eq!(
            got[1].2.class,
            "/Game/Abilities/Comp_Cooldown.Comp_Cooldown_C"
        );
        assert_eq!(got[1].2.class_kind, "package_import");
        assert_eq!(
            got[1].2.native_class,
            "/Script/ShooterGame.EquippableStateMachineComponent"
        );

        assert_eq!(got[2].0, "cdo_subobject");
        assert_eq!(got[2].1, "InventoryComponent");
        assert_eq!(cands[2].outer, "Default__BP_Agent_C");
    }

    /// The row is kept, with the reference it could not follow.
    #[test]
    fn an_unreadable_import_is_a_visible_absence() {
        let script = script();
        let agent = parse_package_header(&character()).unwrap();
        let id = hash_package_name(&agent.name);
        let (cands, _) = scan_header(&agent, id, &script);
        let r = resolve(&cands[1].class, &script, &ClassTable::new());
        assert_eq!(r.class, "?");
        assert_eq!(r.class_kind, "package_import_unresolved");
        assert!(
            r.class_ref.contains("/Game/Abilities/Comp_Cooldown"),
            "{r:?}"
        );
    }

    #[test]
    fn a_numbered_gen_variable_keeps_its_number_and_is_flagged() {
        let script = script();
        let esm = script_index("/Script/ShooterGame.EquippableStateMachineComponent");
        let bytes = build_package(&PackageSpec {
            names: vec!["/Game/P", "X_GEN_VARIABLE"],
            exports: vec![(1, 3, u64::MAX, esm, u64::MAX, 0)],
            ..PackageSpec::default()
        });
        let pkg = parse_package_header(&bytes).unwrap();
        let (cands, _) = scan_header(&pkg, 1, &script);
        assert_eq!(cands.len(), 1);
        assert!(cands[0].numbered);
        assert_eq!(cands[0].instance, "X_2");
        assert_eq!(cands[0].export, "X_GEN_VARIABLE_2");
    }

    /// A package import packs two indices: the imported package in the high
    /// 32 bits of the 62-bit value and the public export hash in the low 32.
    #[test]
    fn a_package_import_splits_into_package_and_hash_indices() {
        let bytes = build_package(&PackageSpec {
            names: vec!["/Game/P"],
            imported_hashes: vec![0xAAAA, 0xBBBB],
            imported_packages: vec![("/Game/First", 0), ("/Game/Second", 0)],
            ..PackageSpec::default()
        });
        let pkg = parse_package_header(&bytes).unwrap();
        let index = (KIND_PACKAGE_IMPORT << 62) | (1u64 << 32) | 1;
        assert_eq!(
            class_ref(&pkg, 1, index),
            ClassRef::Package(
                hash_package_name("/Game/Second"),
                "/Game/Second".to_owned(),
                0xBBBB
            )
        );
    }

    #[test]
    fn an_index_outside_the_package_tables_is_bad_not_guessed() {
        let bytes = build_package(&PackageSpec {
            names: vec!["/Game/P"],
            ..PackageSpec::default()
        });
        let pkg = parse_package_header(&bytes).unwrap();
        let r = class_ref(&pkg, 1, (KIND_PACKAGE_IMPORT << 62) | (5u64 << 32));
        assert!(matches!(r, ClassRef::Bad(_)));
        assert!(matches!(class_ref(&pkg, 1, 4), ClassRef::Bad(_)));
        assert_eq!(class_ref(&pkg, 1, u64::MAX), ClassRef::Null);
    }

    /// The name alone: `/Engine/EngineSounds/Master` in 13.06 is a public
    /// `SoundClass` asset with no super.
    #[test]
    fn an_asset_whose_class_is_named_like_a_metaclass_is_not_a_class() {
        let sound_class = script_index("/Script/Engine.SoundClass");
        let got = classes_of(&PackageSpec {
            names: vec!["/Engine/EngineSounds/Master", "Master"],
            exports: vec![(1, 0, u64::MAX, sound_class, u64::MAX, 0x4444)],
            ..PackageSpec::default()
        });
        assert!(got.is_empty(), "{got:?}");
    }

    /// The super alone: a Blueprint function overriding a native one is public
    /// and has a super, the function it overrides.
    #[test]
    fn a_function_with_a_super_is_not_a_class() {
        let widget = script_index("/Script/UMG.WidgetBlueprintGeneratedClass");
        let parent = script_index("/Script/UMG.UserWidget");
        let function = script_index("/Script/CoreUObject.Function");
        let overridden = script_index("/Script/UMG.UserWidget:Construct");
        let got = classes_of(&PackageSpec {
            names: vec!["/Game/UI/W_Score", "W_Score_C", "Construct"],
            exports: vec![
                (1, 0, u64::MAX, widget, parent, 0x1111),
                (2, 0, 0, function, overridden, 0x2222),
            ],
            ..PackageSpec::default()
        });
        assert_eq!(got, [(0x1111, "/Game/UI/W_Score.W_Score_C".to_owned())]);
    }

    /// Each metaclass the 13.06 containers' classes have, with the class
    /// default object beside the class as in a cooked package.
    #[test]
    fn each_blueprint_metaclass_in_the_game_makes_a_class() {
        for (meta, parent) in [
            (
                "/Script/Engine.BlueprintGeneratedClass",
                "/Script/ShooterGame.EquippableStateMachineComponent",
            ),
            (
                "/Script/UMG.WidgetBlueprintGeneratedClass",
                "/Script/UMG.UserWidget",
            ),
            (
                "/Script/Engine.AnimBlueprintGeneratedClass",
                "/Script/Engine.AnimInstance",
            ),
        ] {
            let (class, parent) = (script_index(meta), script_index(parent));
            let got = classes_of(&PackageSpec {
                names: vec!["/Game/P", "X_C", "Default__X_C"],
                exports: vec![
                    (1, 0, u64::MAX, class, parent, 0x1111),
                    (2, 0, u64::MAX, 0, u64::MAX, 0x2222),
                ],
                ..PackageSpec::default()
            });
            assert_eq!(got, [(0x1111, "/Game/P.X_C".to_owned())], "{meta}");
        }
    }

    /// 31,594 of 13.06's 73,057 class objects have their super in another
    /// package; the rest have a native one.
    #[test]
    fn a_blueprint_whose_parent_is_another_packages_blueprint_is_a_class() {
        let got = classes_of(&cooldown_child());
        assert_eq!(
            got,
            [(
                0x5555,
                "/Game/Abilities/Comp_Cooldown_Child.Comp_Cooldown_Child_C".to_owned()
            )]
        );
    }

    /// 3,265 of 13.06's 26,109 package-import rows reach the native class
    /// through two to five supers.
    #[test]
    fn a_child_blueprint_class_resolves_up_two_supers_to_the_native_class() {
        let script = script();
        let mut table = ClassTable::new();
        for bytes in [cooldown(), build_package(&cooldown_child())] {
            let pkg = parse_package_header(&bytes).unwrap();
            let id = hash_package_name(&pkg.name);
            for (hash, class) in scan_header(&pkg, id, &script).1 {
                table.insert((id, hash), class);
            }
        }
        let child = "/Game/Abilities/Comp_Cooldown_Child";
        let r = resolve(
            &ClassRef::Package(hash_package_name(child), child.to_owned(), 0x5555),
            &script,
            &table,
        );
        assert_eq!(
            r.class,
            "/Game/Abilities/Comp_Cooldown_Child.Comp_Cooldown_Child_C"
        );
        assert_eq!(r.class_kind, "package_import");
        assert_eq!(
            r.native_class,
            "/Script/ShooterGame.EquippableStateMachineComponent"
        );
    }
}
