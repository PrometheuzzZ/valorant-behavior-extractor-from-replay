//! Bounds-checked little-endian reads over a byte slice. The game can change
//! any structure on a patch, so no read trusts a length: a short buffer is an
//! error naming the structure and the offset, never a zero.

use std::fmt;

/// A parse or I/O failure, with enough context to find the byte that caused it.
#[derive(Debug, Clone)]
pub struct Error(pub String);

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

pub type Result<T> = std::result::Result<T, Error>;

pub fn fail<T>(msg: impl Into<String>) -> Result<T> {
    Err(Error(msg.into()))
}

/// A read position over a borrowed buffer; `what` names the structure in errors.
#[derive(Debug, Clone)]
pub struct Cursor<'a> {
    buf: &'a [u8],
    pos: usize,
    what: &'static str,
}

impl<'a> Cursor<'a> {
    pub fn new(buf: &'a [u8], what: &'static str) -> Self {
        Cursor { buf, pos: 0, what }
    }

    /// A cursor starting at `pos`, which must lie inside the buffer (or at its
    /// very end).
    pub fn at(buf: &'a [u8], pos: usize, what: &'static str) -> Result<Self> {
        if pos > buf.len() {
            return fail(format!(
                "{what}: start offset {pos} is past the end of a {}-byte buffer",
                buf.len()
            ));
        }
        Ok(Cursor { buf, pos, what })
    }

    pub fn pos(&self) -> usize {
        self.pos
    }

    pub fn remaining(&self) -> usize {
        self.buf.len() - self.pos
    }

    pub fn take(&mut self, n: usize) -> Result<&'a [u8]> {
        match self.pos.checked_add(n) {
            Some(end) if end <= self.buf.len() => {
                let out = &self.buf[self.pos..end];
                self.pos = end;
                Ok(out)
            }
            _ => fail(format!(
                "{}: need {} bytes at offset {}, only {} left",
                self.what,
                n,
                self.pos,
                self.remaining()
            )),
        }
    }

    pub fn skip(&mut self, n: usize) -> Result<()> {
        self.take(n).map(|_| ())
    }

    pub fn u8(&mut self) -> Result<u8> {
        Ok(self.take(1)?[0])
    }

    pub fn u32(&mut self) -> Result<u32> {
        Ok(le(self.take(4)?) as u32)
    }

    pub fn i32(&mut self) -> Result<i32> {
        Ok(self.u32()? as i32)
    }

    pub fn i64(&mut self) -> Result<i64> {
        Ok(self.u64()? as i64)
    }

    pub fn u64(&mut self) -> Result<u64> {
        Ok(le(self.take(8)?))
    }

    /// An unsigned big-endian integer `n` bytes wide (`n <= 8`). IoStore stores
    /// chunk offsets and lengths as five big-endian bytes each.
    pub fn be_uint(&mut self, n: usize) -> Result<u64> {
        debug_assert!(n <= 8);
        Ok(be(self.take(n)?))
    }

    pub fn le_uint(&mut self, n: usize) -> Result<u64> {
        debug_assert!(n <= 8);
        Ok(le(self.take(n)?))
    }

    /// An element count that must fit what is left of the buffer at
    /// `elem_size` bytes each, refused before anything is allocated for it.
    pub fn count(&mut self, elem_size: usize) -> Result<usize> {
        let at = self.pos;
        let n = self.i32()?;
        if n < 0 {
            return fail(format!("{}: negative count {n} at offset {at}", self.what));
        }
        let n = n as usize;
        if n.saturating_mul(elem_size.max(1)) > self.remaining() {
            return fail(format!(
                "{}: count {n} at offset {at} needs {} bytes, only {} left",
                self.what,
                n.saturating_mul(elem_size.max(1)),
                self.remaining()
            ));
        }
        Ok(n)
    }

    /// An Unreal `FString`: an `i32` length that counts the terminating NUL,
    /// positive for one byte per character and negative for UTF-16. A missing
    /// NUL means the length was read from the wrong place, so it is an error.
    pub fn fstring(&mut self) -> Result<String> {
        let at = self.pos;
        let len = self.i32()?;
        if len == 0 {
            return Ok(String::new());
        }
        if len > 0 {
            let raw = self.take(len as usize)?;
            let (body, nul) = raw.split_at(raw.len() - 1);
            if nul[0] != 0 {
                return fail(format!(
                    "{}: string at offset {at} is not NUL-terminated",
                    self.what
                ));
            }
            return Ok(latin1(body));
        }
        let units = match len.checked_neg() {
            Some(u) => u as usize,
            None => return fail(format!("{}: string length {len} at offset {at}", self.what)),
        };
        let raw = self.take(units.saturating_mul(2))?;
        let code: Vec<u16> = raw
            .chunks_exact(2)
            .map(|p| u16::from_le_bytes([p[0], p[1]]))
            .collect();
        let (body, nul) = code.split_at(code.len() - 1);
        if nul[0] != 0 {
            return fail(format!(
                "{}: UTF-16 string at offset {at} is not NUL-terminated",
                self.what
            ));
        }
        String::from_utf16(body).map_err(|_| {
            Error(format!(
                "{}: invalid UTF-16 string at offset {at}",
                self.what
            ))
        })
    }
}

/// Unreal's one-byte-per-character text: each byte is its own code point.
pub fn latin1(bytes: &[u8]) -> String {
    bytes.iter().map(|&b| b as char).collect()
}

fn le(bytes: &[u8]) -> u64 {
    bytes
        .iter()
        .rev()
        .fold(0u64, |acc, &b| (acc << 8) | u64::from(b))
}

fn be(bytes: &[u8]) -> u64 {
    bytes.iter().fold(0u64, |acc, &b| (acc << 8) | u64::from(b))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn integers_are_little_endian_and_offsets_are_big_endian() {
        let bytes = [0x01, 0x02, 0x03, 0x04, 0x00, 0x00, 0x00, 0x01, 0x02];
        let mut c = Cursor::new(&bytes, "t");
        assert_eq!(c.u32().unwrap(), 0x0403_0201);
        assert_eq!(c.be_uint(5).unwrap(), 0x0102);
        assert_eq!(c.remaining(), 0);
    }

    #[test]
    fn an_overrun_is_an_error_not_a_zero() {
        let bytes = [0x01, 0x02, 0x03];
        let mut c = Cursor::new(&bytes, "short");
        let e = c.u32().unwrap_err();
        assert!(e.0.contains("short"), "{e}");
        assert!(e.0.contains("need 4 bytes"), "{e}");
        // The failed read did not move the cursor.
        assert_eq!(c.pos(), 0);
    }

    #[test]
    fn fstrings_decode_both_widths_and_require_the_terminator() {
        let mut ansi = 4i32.to_le_bytes().to_vec();
        ansi.extend_from_slice(b"abc\0");
        assert_eq!(Cursor::new(&ansi, "t").fstring().unwrap(), "abc");

        let mut wide = (-3i32).to_le_bytes().to_vec();
        for u in [0x48u16, 0x49, 0] {
            wide.extend_from_slice(&u.to_le_bytes());
        }
        assert_eq!(Cursor::new(&wide, "t").fstring().unwrap(), "HI");

        let mut unterminated = 3i32.to_le_bytes().to_vec();
        unterminated.extend_from_slice(b"abc");
        assert!(Cursor::new(&unterminated, "t").fstring().is_err());

        let mut overlong = 9i32.to_le_bytes().to_vec();
        overlong.extend_from_slice(b"abc\0");
        assert!(Cursor::new(&overlong, "t").fstring().is_err());
    }

    #[test]
    fn a_count_larger_than_the_buffer_is_refused_before_allocation() {
        let mut bytes = 1000i32.to_le_bytes().to_vec();
        bytes.extend_from_slice(&[0; 16]);
        assert!(Cursor::new(&bytes, "t").count(4).is_err());
        let neg = (-1i32).to_le_bytes();
        assert!(Cursor::new(&neg, "t").count(1).is_err());
        let mut fits = 4i32.to_le_bytes().to_vec();
        fits.extend_from_slice(&[0; 16]);
        assert_eq!(Cursor::new(&fits, "t").count(4).unwrap(), 4);
    }
}
