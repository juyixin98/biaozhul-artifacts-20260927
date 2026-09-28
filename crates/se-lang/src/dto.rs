//! JSON wire schema and lowering into the validated AST.
//!
//! Wire format — every expression is `{"expr": <tag>, ...}` and every statement is
//! `{"stmt": <tag>, ...}`. Field order is irrelevant.
//!
//! ```json
//! {
//!   "width": 8,
//!   "overflow": "wrap",
//!   "inputs": [{"name": "x", "low": 0, "high": 255}],
//!   "vars":   [{"name": "i", "value": 0}],
//!   "body": [
//!     {"stmt": "assign", "target": "y",
//!      "expr": {"expr": "add", "lhs": {"expr": "var", "name": "x"},
//!                              "rhs": {"expr": "int", "value": 1}}},
//!     {"stmt": "if", "cond": {"expr": "ult", "lhs": "...", "rhs": "..."},
//!      "then": [ ... ], "else": [ ... ]},
//!     {"stmt": "while", "cond": "...", "body": [ ... ]},
//!     {"stmt": "assume", "cond": "..."},
//!     {"stmt": "assert", "cond": "..."}
//!   ]
//! }
//! ```
//!
//! Expression tags: `int` (`value`, any signed integer, reduced mod 2^w), `var`
//! (`name`), binary operator names from [`crate::ast::BinOp`] (`lhs`/`rhs`),
//! `neg`/`not` (`arg`), `ite` (`cond`/`then`/`else`).

use std::collections::BTreeMap;

use serde::de::{self, MapAccess, Visitor};
use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::ast::{
    BinOp, Block, Expr, Input, OverflowMode, Program, Stmt, UnOp, Var, Width,
};

#[derive(Debug, Error)]
pub enum LowerError {
    #[error("invalid program JSON: {0}")]
    Json(#[from] serde_json::Error),
    #[error("unsupported width {0}; expected one of 8, 16, 32, 64")]
    BadWidth(u64),
    #[error("unknown binary operator '{0}'")]
    BadBinOp(String),
    #[error("unknown unary operator '{0}'")]
    BadUnOp(String),
    #[error("invalid overflow mode '{0}'; expected 'wrap' or 'trap'")]
    BadOverflow(String),
    #[error("duplicate declaration of '{0}'")]
    DuplicateName(String),
    #[error("reference to undeclared variable '{0}'")]
    Undeclared(String),
    #[error("cannot assign to input '{0}' (inputs are immutable)")]
    AssignToInput(String),
    #[error(
        "input '{name}' declares domain [{low},{high}] outside {bits}-bit range"
    )]
    DomainOutOfRange {
        name: String,
        low: u64,
        high: u64,
        bits: u32,
    },
    #[error("input '{0}' has empty domain (low > high)")]
    EmptyDomain(String),
    #[error("initial value of var '{0}' exceeds {1}-bit range")]
    VarOutOfRange(String, u32),
    #[error("expression is missing the 'expr' tag")]
    MissingExprTag,
    #[error("statement is missing the 'stmt' tag")]
    MissingStmtTag,
    #[error("unknown statement '{0}'")]
    UnknownStmt(String),
}

// ---------------------------------------------------------------------------
// Wire DTOs
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct InputDto {
    pub name: String,
    #[serde(default)]
    pub low: u64,
    #[serde(default)]
    pub high: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct VarDto {
    pub name: String,
    #[serde(default)]
    pub value: u64,
}

/// Expression wire node. Parsing is implemented manually so one tag namespace can
/// cover literals, variables, operators and ite (an untagged enum fallback would
/// silently misparse otherwise-correct nodes).
#[derive(Clone, Debug, Serialize)]
pub enum ExprDto {
    Int { value: i128 },
    Var { name: String },
    Bin {
        op: String,
        lhs: Box<ExprDto>,
        rhs: Box<ExprDto>,
    },
    Un {
        op: String,
        arg: Box<ExprDto>,
    },
    Ite {
        cond: Box<ExprDto>,
        #[serde(rename = "then")]
        then_e: Box<ExprDto>,
        #[serde(rename = "else")]
        else_e: Box<ExprDto>,
    },
}

impl<'de> Deserialize<'de> for ExprDto {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: de::Deserializer<'de>,
    {
        deserializer.deserialize_map(ExprDtoVisitor)
    }
}

struct ExprDtoVisitor;

impl<'de> Visitor<'de> for ExprDtoVisitor {
    type Value = ExprDto;

    fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        f.write_str("a tagged expression object {\"expr\": <tag>, ...}")
    }

    fn visit_map<M>(self, mut map: M) -> Result<ExprDto, M::Error>
    where
        M: MapAccess<'de>,
    {
        // Buffer all fields as raw values, then dispatch on the tag.
        let mut fields: BTreeMap<String, serde_json::Value> = BTreeMap::new();
        while let Some((key, value)) = map.next_entry::<String, serde_json::Value>()? {
            fields.insert(key, value);
        }
        let tag = fields
            .get("expr")
            .and_then(|v| v.as_str())
            .ok_or_else(|| de::Error::custom("missing 'expr' tag"))?
            .to_string();

        fn take<E: de::Error>(
            fields: &mut BTreeMap<String, serde_json::Value>,
            key: &str,
        ) -> Result<ExprDto, E> {
            let raw = fields
                .remove(key)
                .ok_or_else(|| de::Error::custom(format!("missing field '{key}'")))?;
            serde_json::from_value(raw).map_err(|e| de::Error::custom(e.to_string()))
        }
        fn take_int<E: de::Error>(
            fields: &mut BTreeMap<String, serde_json::Value>,
        ) -> Result<i128, E> {
            let raw = fields
                .remove("value")
                .ok_or_else(|| de::Error::custom("missing field 'value'"))?;
            // Accept numbers and booleans-as-0/1 for convenience.
            if let Some(b) = raw.as_bool() {
                return Ok(b as i128);
            }
            raw.as_i64()
                .map(|v| v as i128)
                .or_else(|| raw.as_u64().map(|v| v as i128))
                .or_else(|| raw.as_f64().map(|v| v as i128))
                .ok_or_else(|| de::Error::custom("'value' must be an integer"))
        }

        Ok(match tag.as_str() {
            "int" | "const" => ExprDto::Int {
                value: take_int(&mut fields)?,
            },
            "var" => {
                let raw = fields
                    .remove("name")
                    .ok_or_else(|| de::Error::custom("missing field 'name'"))?;
                let name = raw
                    .as_str()
                    .ok_or_else(|| de::Error::custom("'name' must be a string"))?;
                ExprDto::Var {
                    name: name.to_string(),
                }
            }
            "neg" | "not" => ExprDto::Un {
                op: tag.to_string(),
                arg: Box::new(take(&mut fields, "arg")?),
            },
            "ite" => ExprDto::Ite {
                cond: Box::new(take(&mut fields, "cond")?),
                then_e: Box::new(take(&mut fields, "then")?),
                else_e: Box::new(take(&mut fields, "else")?),
            },
            // Every other known tag names a binary operator.
            _ => {
                // Validate the name up-front for a targeted error message.
                bin_op(&tag).map_err(|e| de::Error::custom(e.to_string()))?;
                ExprDto::Bin {
                    op: tag.to_string(),
                    lhs: Box::new(take(&mut fields, "lhs")?),
                    rhs: Box::new(take(&mut fields, "rhs")?),
                }
            }
        })
    }
}

#[derive(Clone, Debug, Serialize)]
pub enum StmtDto {
    Assign {
        target: String,
        expr: ExprDto,
    },
    If {
        cond: ExprDto,
        #[serde(rename = "then")]
        then_b: Vec<StmtDto>,
        #[serde(rename = "else")]
        else_b: Vec<StmtDto>,
    },
    While {
        cond: ExprDto,
        body: Vec<StmtDto>,
    },
    Assume {
        cond: ExprDto,
    },
    Assert {
        cond: ExprDto,
    },
}

impl<'de> Deserialize<'de> for StmtDto {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: de::Deserializer<'de>,
    {
        // Buffer the object as JSON so the `stmt` tag can decide the variant; the
        // sub-structures then recurse through their own Deserialize impls.
        let mut raw = serde_json::Value::deserialize(deserializer)?;
        stmt_from_value(&mut raw).map_err(de::Error::custom)
    }
}

fn stmt_from_value(raw: &mut serde_json::Value) -> Result<StmtDto, String> {
    let tag = raw
        .get("stmt")
        .and_then(|v| v.as_str())
        .ok_or("missing 'stmt' tag")?
        .to_string();
    let obj = raw
        .as_object_mut()
        .ok_or("statement must be an object")?;
    obj.remove("stmt");

    let mut field = |key: &str| -> Result<serde_json::Value, String> {
        obj.remove(key).ok_or_else(|| format!("missing field '{key}'"))
    };
    Ok(match tag.as_str() {
        "assign" => StmtDto::Assign {
            target: from(field("target")?)?,
            expr: from(field("expr")?)?,
        },
        "if" => StmtDto::If {
            cond: from(field("cond")?)?,
            then_b: from(obj.remove("then").unwrap_or(serde_json::Value::Array(vec![])))?,
            else_b: from(obj.remove("else").unwrap_or(serde_json::Value::Array(vec![])))?,
        },
        "while" => StmtDto::While {
            cond: from(field("cond")?)?,
            body: from(obj.remove("body").unwrap_or(serde_json::Value::Array(vec![])))?,
        },
        "assume" => StmtDto::Assume {
            cond: from(field("cond")?)?,
        },
        "assert" => StmtDto::Assert {
            cond: from(field("cond")?)?,
        },
        other => return Err(format!("unknown statement '{other}'")),
    })
}

fn from<T: serde::de::DeserializeOwned>(v: serde_json::Value) -> Result<T, String> {
    serde_json::from_value(v).map_err(|e| e.to_string())
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ProgramDto {
    pub width: u64,
    #[serde(default = "default_overflow")]
    pub overflow: String,
    #[serde(default)]
    pub inputs: Vec<InputDto>,
    #[serde(default)]
    pub vars: Vec<VarDto>,
    #[serde(default)]
    pub body: Vec<StmtDto>,
}

fn default_overflow() -> String {
    "wrap".to_string()
}

impl ProgramDto {
    pub fn parse(json: &str) -> Result<(Program, ProgramMeta), LowerError> {
        let dto: ProgramDto = serde_json::from_str(json)?;
        dto.lower()
    }
}

/// Metadata produced alongside lowering (canonical serialization for content ids).
#[derive(Clone, Debug)]
pub struct ProgramMeta {
    pub canonical_json: String,
}

// ---------------------------------------------------------------------------
// Lowering / validation
// ---------------------------------------------------------------------------

struct LowerCtx {
    next_id: usize,
    names: BTreeMap<String, bool>, // true => input, false => state var
}

impl LowerCtx {
    fn fresh_id(&mut self) -> usize {
        let id = self.next_id;
        self.next_id += 1;
        id
    }
}

pub fn bin_op(name: &str) -> Result<BinOp, LowerError> {
    use BinOp::*;
    Ok(match name {
        "add" | "+" => Add,
        "sub" | "-" => Sub,
        "mul" | "*" => Mul,
        "udiv" | "/" => Udiv,
        "urem" | "%" | "umod" => Urem,
        "sdiv" => Sdiv,
        "srem" | "smod" => Srem,
        "and" | "&" => And,
        "or" | "|" => Or,
        "xor" | "^" => Xor,
        "shl" | "<<" => Shl,
        "lshr" | "ushr" | ">>" => LShr,
        "ashr" => AShr,
        "eq" | "==" => Eq,
        "ne" | "!=" => Ne,
        "ult" | "<" => Ult,
        "ule" | "<=" => Ule,
        "ugt" | ">" => Ugt,
        "uge" | ">=" => Uge,
        "slt" => Slt,
        "sle" => Sle,
        "sgt" => Sgt,
        "sge" => Sge,
        other => return Err(LowerError::BadBinOp(other.to_string())),
    })
}

pub fn un_op(name: &str) -> Result<UnOp, LowerError> {
    Ok(match name {
        "neg" | "-" => UnOp::Neg,
        "not" | "~" => UnOp::Not,
        other => return Err(LowerError::BadUnOp(other.to_string())),
    })
}

fn lower_expr(dto: &ExprDto, ctx: &mut LowerCtx) -> Result<Expr, LowerError> {
    Ok(match dto {
        ExprDto::Int { value } => {
            // Signed literals are reduced modulo 2^w during evaluation.
            Expr::Int((*value as u128) as u64)
        }
        ExprDto::Var { name } => {
            if !ctx.names.contains_key(name) {
                return Err(LowerError::Undeclared(name.clone()));
            }
            Expr::Var(name.clone())
        }
        ExprDto::Bin { op, lhs, rhs } => Expr::Bin {
            op: bin_op(op)?,
            lhs: Box::new(lower_expr(lhs, ctx)?),
            rhs: Box::new(lower_expr(rhs, ctx)?),
        },
        ExprDto::Un { op, arg } => Expr::Un {
            op: un_op(op)?,
            arg: Box::new(lower_expr(arg, ctx)?),
        },
        ExprDto::Ite {
            cond,
            then_e,
            else_e,
        } => Expr::Ite {
            cond: Box::new(lower_expr(cond, ctx)?),
            then: Box::new(lower_expr(then_e, ctx)?),
            els: Box::new(lower_expr(else_e, ctx)?),
        },
    })
}

fn lower_block(dtos: &[StmtDto], ctx: &mut LowerCtx) -> Result<Block, LowerError> {
    let mut out = Vec::with_capacity(dtos.len());
    for s in dtos {
        out.push(lower_stmt(s, ctx)?);
    }
    Ok(Block::new(out))
}

fn lower_stmt(dto: &StmtDto, ctx: &mut LowerCtx) -> Result<Stmt, LowerError> {
    Ok(match dto {
        StmtDto::Assign { target, expr } => {
            if let Some(true) = ctx.names.get(target).copied() {
                return Err(LowerError::AssignToInput(target.clone()));
            }
            // Assignment is allowed to introduce an implicit state variable.
            ctx.names.entry(target.clone()).or_insert(false);
            Stmt::Assign {
                id: ctx.fresh_id(),
                target: target.clone(),
                expr: lower_expr(expr, ctx)?,
            }
        }
        StmtDto::If {
            cond,
            then_b,
            else_b,
        } => Stmt::If {
            id: ctx.fresh_id(),
            cond: lower_expr(cond, ctx)?,
            then_blk: lower_block(then_b, ctx)?,
            else_blk: lower_block(else_b, ctx)?,
        },
        StmtDto::While { cond, body } => Stmt::While {
            id: ctx.fresh_id(),
            cond: lower_expr(cond, ctx)?,
            body: lower_block(body, ctx)?,
        },
        StmtDto::Assume { cond } => Stmt::Assume {
            id: ctx.fresh_id(),
            cond: lower_expr(cond, ctx)?,
        },
        StmtDto::Assert { cond } => Stmt::Assert {
            id: ctx.fresh_id(),
            cond: lower_expr(cond, ctx)?,
        },
    })
}

impl ProgramDto {
    /// Validate and lower the DTO into the immutable [`Program`] AST.
    pub fn lower(&self) -> Result<(Program, ProgramMeta), LowerError> {
        let width = Width::from_bits(self.width).ok_or(LowerError::BadWidth(self.width))?;
        let overflow = match self.overflow.as_str() {
            "wrap" => OverflowMode::Wrap,
            "trap" => OverflowMode::Trap,
            other => return Err(LowerError::BadOverflow(other.to_string())),
        };
        let mask = width.mask_u64();

        let mut names = BTreeMap::new();
        let mut inputs = Vec::new();
        for i in &self.inputs {
            if names.contains_key(&i.name) {
                return Err(LowerError::DuplicateName(i.name.clone()));
            }
            if i.low > mask || i.high > mask {
                return Err(LowerError::DomainOutOfRange {
                    name: i.name.clone(),
                    low: i.low,
                    high: i.high,
                    bits: width.bits(),
                });
            }
            if i.low > i.high {
                return Err(LowerError::EmptyDomain(i.name.clone()));
            }
            names.insert(i.name.clone(), true);
            inputs.push(Input {
                name: i.name.clone(),
                low: i.low,
                high: i.high,
            });
        }

        let mut vars = Vec::new();
        for v in &self.vars {
            if names.contains_key(&v.name) {
                return Err(LowerError::DuplicateName(v.name.clone()));
            }
            if v.value > mask {
                return Err(LowerError::VarOutOfRange(v.name.clone(), width.bits()));
            }
            names.insert(v.name.clone(), false);
            vars.push(Var {
                name: v.name.clone(),
                init: v.value,
            });
        }

        let mut lctx = LowerCtx { next_id: 0, names };
        let body = lower_block(&self.body, &mut lctx)?;

        let canonical_json =
            serde_json::to_string(self).expect("ProgramDto is always serializable");

        Ok((
            Program {
                width,
                overflow,
                inputs,
                vars,
                body,
                stmt_count: lctx.next_id,
            },
            ProgramMeta { canonical_json },
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_bad_width() {
        let dto = ProgramDto {
            width: 7,
            overflow: "wrap".into(),
            inputs: vec![],
            vars: vec![],
            body: vec![],
        };
        assert!(matches!(dto.lower(), Err(LowerError::BadWidth(7))));
    }

    #[test]
    fn rejects_undeclared_var_and_input_assign() {
        let dto: ProgramDto = serde_json::from_str(
            r#"{"width":8,"inputs":[{"name":"x"}],"body":[
              {"stmt":"assign","target":"y","expr":{"expr":"var","name":"z"}}]}"#,
        )
        .unwrap();
        assert!(matches!(dto.lower(), Err(LowerError::Undeclared(n)) if n == "z"));

        let dto: ProgramDto = serde_json::from_str(
            r#"{"width":8,"inputs":[{"name":"x"}],"body":[
              {"stmt":"assign","target":"x","expr":{"expr":"int","value":1}}]}"#,
        )
        .unwrap();
        assert!(matches!(dto.lower(), Err(LowerError::AssignToInput(n)) if n == "x"));
    }

    #[test]
    fn assigns_unique_preorder_ids() {
        let (p, _) = ProgramDto::parse(
            r#"{"width":8,"inputs":[{"name":"x","low":0,"high":1}],"body":[
              {"stmt":"if","cond":{"expr":"var","name":"x"},
               "then":[{"stmt":"assert","cond":{"expr":"int","value":1}}],
               "else":[{"stmt":"assert","cond":{"expr":"int","value":0}}]},
              {"stmt":"assert","cond":{"expr":"int","value":1}}]}"#,
        )
        .unwrap();
        assert_eq!(p.stmt_count, 4);
        assert_eq!(p.body[0].id(), 0);
        if let Stmt::If {
            then_blk,
            else_blk,
            ..
        } = &p.body[0]
        {
            assert_eq!(then_blk[0].id(), 1);
            assert_eq!(else_blk[0].id(), 2);
        } else {
            panic!("expected if");
        }
        assert_eq!(p.body[1].id(), 3);
    }

    #[test]
    fn parses_ite_and_nested_exprs() {
        let dto: ProgramDto = serde_json::from_str(
            r#"{"width":16,"inputs":[{"name":"x"}],"body":[
              {"stmt":"assert","cond":{"expr":"ite","cond":{"expr":"var","name":"x"},
                "then":{"expr":"int","value":1},"else":{"expr":"int","value":1}}}]}"#,
        )
        .unwrap();
        let (p, _) = dto.lower().unwrap();
        assert_eq!(p.stmt_count, 1);
    }
}
