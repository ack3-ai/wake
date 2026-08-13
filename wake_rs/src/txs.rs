use std::collections::VecDeque;

use pyo3::{exceptions::PyIndexError, prelude::*};

use crate::chain::HistoryPrunedError;
use crate::tx::TransactionAbc;

/// A retained transaction together with a copy of its journal index.
///
/// The index is duplicated out of `TransactionAbc` so that reading it is a plain
/// `usize`: reaching through `Py<TransactionAbc>` needs a `Python` token and a
/// `RefCell` borrow, which is the wrong cost on the journal-floor path that runs
/// after every transaction, and would infect every signature with `py`.
pub(crate) struct TxSlot {
    pub(crate) journal_index: usize,
    pub(crate) tx: Py<TransactionAbc>,
}

#[pyclass]
pub struct Txs {
    /// Retained window, oldest first.
    txs: VecDeque<TxSlot>,
    /// Absolute index of `txs.front()`, i.e. how many transactions have been
    /// dropped from the front. Indices exposed to Python are absolute, so this
    /// is what `__getitem__` subtracts.
    start_index: usize,
}

impl Txs {
    pub(crate) fn new() -> Self {
        Self {
            txs: VecDeque::new(),
            start_index: 0,
        }
    }

    pub(crate) fn add_tx(&mut self, journal_index: usize, tx: Py<TransactionAbc>) {
        // `remove_txs` and the journal floor both assume the window is sorted by
        // journal index. It holds because every committed transaction touches
        // the caller (nonce and gas), so each one appends at least one entry.
        debug_assert!(
            self.txs
                .back()
                .is_none_or(|last| last.journal_index <= journal_index),
            "transaction journal indices must be non-decreasing"
        );
        self.txs.push_back(TxSlot { journal_index, tx });
    }

    /// Lowest journal index the retained transactions still need to replay.
    /// `None` when the window is empty. O(1) — never a scan.
    pub(crate) fn oldest_journal_index(&self) -> Option<usize> {
        self.txs.front().map(|slot| slot.journal_index)
    }

    /// Drops transactions below `journal_index`, returning them so the caller
    /// decides when the Python objects die. Dropping the last reference to a
    /// transaction can run `__del__`, which can re-enter the chain, so that must
    /// not happen while `Chain` is mutably borrowed.
    ///
    /// Retention is decided by blocks, not by a transaction count: passing the
    /// journal index of the newest *dropped* block drops exactly the transactions
    /// that were in dropped blocks. A block's index is recorded after it and a
    /// transaction's before it, so the transactions of block `n` occupy
    /// `[index(n-1), index(n))`. Transactions not yet in a block sit above the
    /// newest block's index and are therefore always kept.
    pub(crate) fn drain_below(&mut self, journal_index: usize) -> Vec<TxSlot> {
        let drop_count = self
            .txs
            .partition_point(|slot| slot.journal_index < journal_index);
        self.start_index += drop_count;
        self.txs.drain(..drop_count).collect()
    }

    /// Drops every transaction, returning them for the caller to drop.
    pub(crate) fn drain_all(&mut self) -> Vec<TxSlot> {
        self.start_index += self.txs.len();
        self.txs.drain(..).collect()
    }

    /// Drops transactions at or after `journal_index` (used when reverting).
    pub(crate) fn remove_txs(&mut self, journal_index: usize) -> Vec<TxSlot> {
        // Leftmost slot with `journal_index >= journal_index`; unlike
        // `binary_search_by` this is unambiguous if two slots ever tie.
        let left = self
            .txs
            .partition_point(|slot| slot.journal_index < journal_index);
        self.txs.split_off(left).into_iter().collect()
    }

    /// Re-seeds the window from a snapshot after reverting to it. The snapshot
    /// does not pin the journal; replay getters validate each restored journal
    /// point at use through `CacheDB::rollback`.
    pub(crate) fn reseed(&mut self, start_index: usize, slots: Vec<TxSlot>) {
        debug_assert!(self.txs.is_empty(), "reseed expects a drained window");
        self.start_index = start_index;
        self.txs = slots.into();
    }

    /// Absolute index of the oldest retained transaction.
    pub(crate) fn start_index(&self) -> usize {
        self.start_index
    }

    pub(crate) fn iter(&self) -> impl Iterator<Item = &TxSlot> {
        self.txs.iter()
    }
}

#[pymethods]
impl Txs {
    /// Number of transactions in the current canonical history, pruned ones
    /// included — positive indices are absolute, so this is what makes
    /// `chain.txs[len(chain.txs) - 1]` mean the newest transaction.
    fn __len__(&self) -> usize {
        self.start_index + self.txs.len()
    }

    /// Absolute index of the oldest transaction still retained.
    #[getter]
    fn first_index(&self) -> usize {
        self.start_index
    }

    /// Absolute index of the newest transaction in the current canonical history,
    /// or `None` if there are none.
    ///
    /// Note this is not the newest *retained* one, so this and `first_index` are not
    /// the two ends of a range: once the window empties, `first_index` can exceed
    /// this.
    #[getter]
    fn last_index(&self) -> Option<usize> {
        (self.start_index + self.txs.len()).checked_sub(1)
    }

    fn __getitem__(&self, py: Python, index: isize) -> PyResult<Py<TransactionAbc>> {
        let total = self.start_index + self.txs.len();
        let absolute = if index < 0 {
            // Negative indices count back from the newest transaction, which is
            // the dominant usage and is unaffected by pruning.
            match total.checked_sub(index.unsigned_abs()) {
                Some(absolute) => absolute,
                None => {
                    return Err(PyIndexError::new_err(format!(
                        "index {index} out of range"
                    )))
                }
            }
        } else {
            let absolute = index as usize;
            if absolute >= total {
                return Err(PyIndexError::new_err(format!(
                    "index {index} out of range"
                )));
            }
            absolute
        };

        match absolute.checked_sub(self.start_index) {
            Some(offset) if offset < self.txs.len() => {
                Ok(self.txs[offset].tx.clone_ref(py))
            }
            // In range but no longer retained: an expected condition, not a
            // caller error, so it gets its own exception type.
            _ => Err(HistoryPrunedError::new_err(format!(
                "transaction {absolute} is no longer retained: transactions {}..{} are available. \
                 Raise `testing.block_history` in the configuration (or set it to null to \
                 disable pruning) to keep more.",
                self.start_index, total
            ))),
        }
    }
}
