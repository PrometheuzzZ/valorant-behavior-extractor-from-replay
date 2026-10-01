//! Name batches and `FMappedName`. Every IoStore name table (a package's name
//! map, its imported package names, the script object names in `global.ucas`)
//! is a name batch: a count, a string-byte total, a hash algorithm version, a
//! u64 hash and a two-byte header per name, then the strings back to back.

use crate::reader::{Cursor, Result, fail, latin1};

/// The hash algorithm version in every batch of the shipped game: the layout's
/// one fixed value, so finding it confirms the batch starts where expected.
pub const NAME_HASH_VERSION: u64 = 0xC164_0000;

/// Read one name batch, consuming its string block exactly: a header that runs
/// past it, or bytes left over, is an error rather than a shorter table.
pub fn read_name_batch(c: &mut Cursor<'_>) -> Result<Vec<String>> {
    let at = c.pos();
    let num = c.u32()? as usize;
    if num == 0 {
        return Ok(Vec::new());
    }
    let string_bytes = c.u32()? as usize;
    let version = c.u64()?;
    if version != NAME_HASH_VERSION {
        return fail(format!(
            "name batch at offset {at}: hash version {version:#x}, expected {NAME_HASH_VERSION:#x}"
        ));
    }
    c.skip(num.saturating_mul(8))?;
    let headers = c.take(num.saturating_mul(2))?;
    let strings = c.take(string_bytes)?;

    let mut out = Vec::with_capacity(num);
    let mut p = 0usize;
    for (i, h) in headers.chunks_exact(2).enumerate() {
        let wide = h[0] & 0x80 != 0;
        let len = (usize::from(h[0] & 0x7f) << 8) | usize::from(h[1]);
        // Not aligned: a UTF-16 name starts where the previous one ended (26
        // packages of 13.06 have one at an odd offset).
        let (end, unit) = if wide {
            (p + len * 2, "UTF-16 units")
        } else {
            (p + len, "bytes")
        };
        if end > strings.len() {
            return fail(format!(
                "name batch at offset {at}: name {i} ({len} {unit}) overruns the {}-byte string block",
                strings.len()
            ));
        }
        let raw = &strings[p..end];
        out.push(if wide {
            let units: Vec<u16> = raw
                .chunks_exact(2)
                .map(|u| u16::from_le_bytes([u[0], u[1]]))
                .collect();
            String::from_utf16(&units).or_else(|_| {
                fail(format!(
                    "name batch at offset {at}: name {i} is not valid UTF-16"
                ))
            })?
        } else {
            latin1(raw)
        });
        p = end;
    }
    if p != strings.len() {
        return fail(format!(
            "name batch at offset {at}: {} of {} string bytes unused",
            strings.len() - p,
            strings.len()
        ));
    }
    Ok(out)
}

/// An `FMappedName`: a 30-bit index into some name table (the top two bits are
/// the table kind), and the FName instance number.
#[derive(Debug, Clone, Copy)]
pub struct MappedName {
    pub index: u32,
    pub number: u32,
}

impl MappedName {
    pub fn read(c: &mut Cursor<'_>) -> Result<Self> {
        let raw = c.u32()?;
        let number = c.u32()?;
        Ok(MappedName {
            index: raw & 0x3fff_ffff,
            number,
        })
    }

    /// The name as the wire spells it. The instance number is part of the
    /// name: 0 means no suffix and N means `_{N-1}`; dropping it merges
    /// distinct names (`AresAttributeSet_1` is `AresAttributeSet` number 2).
    pub fn render(&self, names: &[String]) -> Result<String> {
        let base = self.base(names)?;
        Ok(with_number(base, self.number))
    }

    /// The table string alone, without the instance number.
    pub fn base<'n>(&self, names: &'n [String]) -> Result<&'n str> {
        match names.get(self.index as usize) {
            Some(s) => Ok(s.as_str()),
            None => fail(format!(
                "name index {} out of range for a {}-entry name table",
                self.index,
                names.len()
            )),
        }
    }
}

/// Append an FName instance number the way Unreal displays it.
pub fn with_number(base: &str, number: u32) -> String {
    if number == 0 {
        base.to_owned()
    } else {
        format!("{base}_{}", number - 1)
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    /// Serialize `names` the way the game does, for tests elsewhere too.
    /// Wide names are marked with a leading `~`, which is stripped.
    pub fn name_batch(names: &[&str]) -> Vec<u8> {
        let mut headers = Vec::new();
        let mut strings: Vec<u8> = Vec::new();
        for n in names {
            if let Some(w) = n.strip_prefix('~') {
                let units: Vec<u16> = w.encode_utf16().collect();
                headers.push(0x80 | (units.len() >> 8) as u8);
                headers.push(units.len() as u8);
                for u in units {
                    strings.extend_from_slice(&u.to_le_bytes());
                }
            } else {
                headers.push((n.len() >> 8) as u8);
                headers.push(n.len() as u8);
                strings.extend_from_slice(n.as_bytes());
            }
        }
        let mut out = Vec::new();
        out.extend_from_slice(&(names.len() as u32).to_le_bytes());
        if names.is_empty() {
            return out;
        }
        out.extend_from_slice(&(strings.len() as u32).to_le_bytes());
        out.extend_from_slice(&NAME_HASH_VERSION.to_le_bytes());
        out.extend(std::iter::repeat_n(0u8, names.len() * 8));
        out.extend_from_slice(&headers);
        out.extend_from_slice(&strings);
        out
    }

    #[test]
    fn a_batch_round_trips_including_a_wide_name() {
        let bytes = name_batch(&["abc", "~wide", "x"]);
        let mut c = Cursor::new(&bytes, "t");
        assert_eq!(read_name_batch(&mut c).unwrap(), ["abc", "wide", "x"]);
        assert_eq!(c.remaining(), 0);
    }

    /// A UTF-16 name right after an odd-length ANSI one, which two-byte
    /// alignment would read one byte late.
    #[test]
    fn a_wide_name_at_an_odd_offset_is_read_where_it_starts() {
        let wide = "~\u{8d34}\u{56fe} #5";
        let bytes = name_batch(&["PhysXPC", wide, "BodySetup"]);
        // "PhysXPC" is 7 bytes, so the wide name starts at an odd offset.
        let got = read_name_batch(&mut Cursor::new(&bytes, "t")).unwrap();
        assert_eq!(got, ["PhysXPC", &wide[1..], "BodySetup"]);
    }

    #[test]
    fn an_empty_batch_is_four_bytes() {
        let bytes = name_batch(&[]);
        assert_eq!(bytes.len(), 4);
        let mut c = Cursor::new(&bytes, "t");
        assert!(read_name_batch(&mut c).unwrap().is_empty());
    }

    #[test]
    fn a_header_that_overruns_the_strings_is_an_error() {
        let mut bytes = name_batch(&["abc", "de"]);
        // Claim the second name is 3 bytes long; the block only holds 2.
        let hdr = 4 + 4 + 8 + 2 * 8;
        bytes[hdr + 3] = 3;
        let err = read_name_batch(&mut Cursor::new(&bytes, "t")).unwrap_err();
        assert!(err.0.contains("overruns"), "{err}");
    }

    #[test]
    fn unused_string_bytes_are_an_error() {
        let mut bytes = name_batch(&["abc", "de"]);
        let hdr = 4 + 4 + 8 + 2 * 8;
        bytes[hdr + 3] = 1;
        let err = read_name_batch(&mut Cursor::new(&bytes, "t")).unwrap_err();
        assert!(err.0.contains("unused"), "{err}");
    }

    #[test]
    fn a_wrong_hash_version_is_an_error() {
        let mut bytes = name_batch(&["abc"]);
        bytes[8] ^= 1;
        assert!(read_name_batch(&mut Cursor::new(&bytes, "t")).is_err());
    }

    #[test]
    fn the_instance_number_is_part_of_the_name() {
        let names = vec!["AresAttributeSet".to_owned()];
        let n = |number| MappedName { index: 0, number };
        assert_eq!(n(0).render(&names).unwrap(), "AresAttributeSet");
        assert_eq!(n(1).render(&names).unwrap(), "AresAttributeSet_0");
        assert_eq!(n(2).render(&names).unwrap(), "AresAttributeSet_1");
        let bad = MappedName {
            index: 1,
            number: 0,
        };
        assert!(bad.render(&names).is_err());
    }
}
