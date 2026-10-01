//! Unreal's name hashes: CityHash64 v1.1 over lowercased UTF-16LE text. A
//! package import's package id is `hash_package_name(name)`, which keys
//! `ClassTable`; `hash_path` rebuilds script indices as a self-check (`.` and
//! `:` fold to `/`, so it cannot check separators).

/// The 62 bits an `FPackageObjectIndex` keeps of a hash.
pub const INDEX_MASK: u64 = (1u64 << 62) - 1;

/// A script object's global index: its path hashed, top two bits cleared.
pub fn hash_path(path: &str) -> u64 {
    hash_package_name(&path.replace(['.', ':'], "/")) & INDEX_MASK
}

/// A package's id, all 64 bits: an `ExportBundleData` chunk id carries it.
pub fn hash_package_name(name: &str) -> u64 {
    let lowered: String = name.chars().map(lower).collect();
    let bytes: Vec<u8> = lowered.encode_utf16().flat_map(u16::to_le_bytes).collect();
    cityhasher::hash(bytes)
}

/// One-to-one lowercasing: a character whose lowercase form is several is left
/// alone, so the UTF-16 length never changes and a wrong guess shows as a
/// counted hash mismatch, never an accidental match.
fn lower(ch: char) -> char {
    if ch.is_ascii() {
        return ch.to_ascii_lowercase();
    }
    let mut it = ch.to_lowercase();
    match (it.next(), it.next()) {
        (Some(l), None) => l,
        _ => ch,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Reference values from the game, not from this code: the global index a
    /// 13.06 `global.ucas` stores for each engine path, and the chunk id a 13.06
    /// container stores for the package. They cover the 17-32, 33-64 and
    /// over-64 byte classes (UTF-16 doubles the length), all a real path reaches.
    #[test]
    fn engine_paths_hash_to_the_indices_the_game_stores() {
        for (path, stored) in [
            ("/Script/UMG", 0x7320_1665_2744_f78bu64),
            ("/Script/Engine", 0x51ac_ced3_dc7c_0922),
            ("/Script/CoreUObject", 0x61fe_bf02_cdde_2af3),
            ("/Script/ShooterGame.AresInventory", 0x6106_a046_0675_78c9),
            (
                "/Script/ShooterGame.EquippableStateMachineComponent",
                0x6102_9df2_5193_6cb5,
            ),
        ] {
            assert_eq!(hash_path(path), stored & INDEX_MASK, "{path}");
        }
        assert_eq!(
            hash_package_name("/Game/Labels/Premier/Rewards/V25A5PremierRewards_Label"),
            0x8b5e_0d50_90f3_bdb6
        );
    }

    /// Every length class of CityHash64 v1.1, the short ones included, which
    /// no real path reaches: a hash of another CityHash version fails here.
    #[test]
    fn each_length_class_is_pinned() {
        let text = b"/script/shootergame/equippablestatemachinecomponent/abcdefghijklmnopqrstuvwxyz0123456789";
        for (len, want) in [
            (0usize, 0x9ae1_6a3b_2f90_404fu64),
            (1, 0x1d4e_410d_a3ef_3e4b),
            (3, 0x609a_9da5_a6c0_eec8),
            (4, 0x24d2_976a_1996_47b8),
            (7, 0x221a_f868_e174_c75d),
            (8, 0xe878_e99b_36c2_0cb7),
            (16, 0xd088_f403_a5d3_3e96),
            (17, 0x66f4_9037_d4fc_7801),
            (32, 0xcb22_9a93_da5a_ae3b),
            (33, 0xfe75_e342_58fe_c8aa),
            (64, 0x1ad2_eeb6_73b7_b30d),
            (65, 0x8109_8aa1_8df7_c6c2),
            (88, 0x1aa8_aaa4_7c22_b4e7),
        ] {
            assert_eq!(cityhasher::hash::<u64>(&text[..len]), want, "length {len}");
        }
    }

    #[test]
    fn path_hashing_folds_case_and_separators() {
        assert_eq!(
            hash_path("/Script/ShooterGame.AresInventory"),
            hash_path("/script/shootergame/aresinventory")
        );
        assert_ne!(
            hash_path("/Script/ShooterGame.AresInventory"),
            hash_path("/Script/ShooterGame.AresInventor")
        );
        assert_eq!(hash_path("/Script/X.Y") >> 62, 0);
    }
}
