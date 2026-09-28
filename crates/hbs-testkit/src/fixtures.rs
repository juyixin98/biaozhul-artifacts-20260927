//! On-disk reusable fixtures.
//!
//! Fixtures are generated deterministically into a directory:
//!
//! * `<label>.values.json` — the sorted unique `u32` values (input);
//! * `<label>.hbs` — the encoded set produced by the SUT encoder, used as a
//!   round-trip / corruption target;
//! * `<label>.meta.json` — expected cardinality and chunk statistics.
//!
//! The values are the ground truth; they are regenerable from `(distribution,
//! seed)` and are never produced by decoding the SUT file, so a broken
//! encoder cannot poison the expected answers.

use std::path::Path;

use hbs_format::encode;

use crate::generator::{Distribution, Sample, fixture_samples};
use crate::props::sut_from;

/// Fixture manifest entry metadata.
#[derive(Debug, Clone)]
pub struct FixtureMeta {
    /// Scenario label.
    pub label: String,
    /// Distribution name.
    pub distribution: String,
    /// Generator seed.
    pub seed: u64,
    /// Expected cardinality.
    pub cardinality: u64,
    /// Expected non-empty chunk count.
    pub chunks: usize,
    /// Expected array-container count.
    pub array_containers: usize,
    /// Expected bitmap-container count.
    pub bitmap_containers: usize,
    /// Smallest value.
    pub min: u32,
    /// Largest value.
    pub max: u32,
}

// Flat JSON rendering for the fixture manifest (avoids a serde dependency
// in the testkit).
impl FixtureMeta {
    fn to_json(&self) -> String {
        format!(
            "{{\n  \"label\": {:?},\n  \"distribution\": {:?},\n  \"seed\": {},\n  \
             \"cardinality\": {},\n  \"chunks\": {},\n  \"array_containers\": {},\n  \
             \"bitmap_containers\": {},\n  \"min\": {},\n  \"max\": {}\n}}\n",
            self.label,
            self.distribution,
            self.seed,
            self.cardinality,
            self.chunks,
            self.array_containers,
            self.bitmap_containers,
            self.min,
            self.max,
        )
    }
}

fn values_json(values: &[u32]) -> String {
    let mut s = String::from("[\n");
    for (i, v) in values.iter().enumerate() {
        if i % 16 == 0 {
            s.push_str("  ");
        }
        s.push_str(&v.to_string());
        if i + 1 < values.len() {
            s.push(',');
        }
        if i % 16 == 15 || i + 1 == values.len() {
            s.push('\n');
        } else {
            s.push(' ');
        }
    }
    s.push_str("]\n");
    s
}

/// Generate every fixture into `dir`, returning the metadata list.
pub fn generate_fixtures(dir: impl AsRef<Path>) -> std::io::Result<Vec<FixtureMeta>> {
    let dir = dir.as_ref();
    std::fs::create_dir_all(dir)?;
    let mut metas = Vec::new();
    for sample in fixture_samples() {
        let Sample {
            label,
            values,
            seed,
            distribution,
        } = &sample;
        std::fs::write(
            dir.join(format!("{label}.values.json")),
            values_json(values),
        )?;

        let set = sut_from(values);
        let encoded = encode(&set);
        std::fs::write(dir.join(format!("{label}.hbs")), encoded)?;

        let stats = set.stats();
        let meta = FixtureMeta {
            label: label.clone(),
            distribution: distribution.as_str().to_string(),
            seed: *seed,
            cardinality: set.len(),
            chunks: stats.chunks,
            array_containers: stats.array_containers,
            bitmap_containers: stats.bitmap_containers,
            min: set.min().unwrap_or(0),
            max: set.max().unwrap_or(0),
        };
        std::fs::write(dir.join(format!("{label}.meta.json")), meta.to_json())?;
        metas.push(meta);
    }
    Ok(metas)
}

/// Parse the flat values JSON produced by [`values_json`]. It only needs to
/// understand digits, commas and brackets — the generator wrote it.
pub fn parse_values_json(text: &str) -> Vec<u32> {
    text.split(|c: char| !c.is_ascii_digit())
        .filter(|s| !s.is_empty())
        .map(|s| s.parse().expect("fixture value parse"))
        .collect()
}

/// Expected container-choice per chunk for the threshold sample:
/// chunk 0 = 4095 (array), chunk 1 = 4096 (array), chunk 2 = 4097 (bitmap).
pub fn expected_threshold_container_kinds() -> Vec<(u16, &'static str)> {
    vec![(0, "array"), (1, "array"), (2, "bitmap")]
}

/// Distribution list as strings, for CLI/manifest output.
pub fn distribution_names() -> [&'static str; 4] {
    [
        Distribution::Sparse.as_str(),
        Distribution::Dense.as_str(),
        Distribution::Interleaved.as_str(),
        Distribution::ContainerThreshold.as_str(),
    ]
}
