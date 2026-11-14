use std::collections::{HashMap, VecDeque};

use pyo3::exceptions::{PyNotImplementedError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use revm::context::{BlockEnv, ContextTr};
use revm::primitives::B256;

use crate::chain::{BlockInfo, Chain, HistoryPrunedError, ProviderWrapper};
use crate::tx::TransactionAbc;
use crate::memory_db::JournalPoint;
use crate::enums::BlockEnum;
use crate::globals::TOKIO_RUNTIME;
use crate::utils::header_to_block_env;
use log::info;

/// A retained block together with a copy of its journal index and number, for
/// the same reason as [`crate::txs::TxSlot`]: the journal floor is read after
/// every transaction and must not need a `Python` token or a `RefCell` borrow.
pub(crate) struct BlockSlot {
    pub(crate) journal_index: usize,
    pub(crate) number: u64,
    pub(crate) block: Py<Block>,
}

#[pyclass]
pub(crate) struct Blocks {
    chain: Option<Py<Chain>>,
    /// Retained window, oldest first.
    pub(crate) blocks: VecDeque<BlockSlot>,
    /// Block number of `blocks.front()`.
    blocks_start_index: usize,
    forked_blocks: HashMap<u64, Py<Block>>,
    forked_block: Option<u64>,
}

impl Blocks {
    pub fn add_block(
        &mut self,
        py: Python,
        block_env: BlockEnv, // must always have gas_limit set to the initial value
        journal_index: JournalPoint,
        block_hash: B256,
        gas_used: u64,
    ) -> PyResult<Py<Block>> {
        let block_number: usize = block_env.number.try_into().unwrap();
        let block = Py::new(
            py,
            Block {
                chain: self.chain.as_ref().ok_or_else(|| {
                    HistoryPrunedError::new_err("block history was discarded on disconnect")
                })?.clone_ref(py),
                block_hash,
                block_env,
                journal_index: Some(journal_index),
                gas_used,
            },
        )?;
        // An `assert!` here would cross the pyo3 boundary as `PanicException`,
        // which subclasses `BaseException` and slips past `except Exception`.
        if self.blocks.len() + self.blocks_start_index != block_number {
            return Err(PyRuntimeError::new_err(format!(
                "block window desynchronized: expected block number {}, got {block_number}",
                self.blocks.len() + self.blocks_start_index
            )));
        }
        self.blocks.push_back(BlockSlot {
            journal_index: journal_index.index,
            number: block_number as u64,
            block: block.clone_ref(py),
        });
        Ok(block)
    }

    /// Lowest journal index the retained blocks still need to replay.
    /// `None` when the window is empty. O(1) — never a scan.
    pub(crate) fn oldest_journal_index(&self) -> Option<usize> {
        self.blocks.front().map(|slot| slot.journal_index)
    }

    pub(crate) fn len(&self) -> usize {
        self.blocks.len()
    }

    /// Block number of the oldest locally mined block retained for replay.
    pub(crate) fn start_index(&self) -> usize {
        self.blocks_start_index
    }

    /// Drops blocks from the front until at most `keep` remain, returning them so
    /// the caller controls when the Python objects die. See
    /// [`crate::txs::Txs::drain_below`].
    pub(crate) fn drain_front(&mut self, keep: usize) -> Vec<BlockSlot> {
        let drop_count = self.blocks.len().saturating_sub(keep);
        self.blocks_start_index += drop_count;
        self.blocks.drain(..drop_count).collect()
    }

    /// Detach a disconnected history, including references held by callers that
    /// saved the collection itself. Drop the returned objects outside the chain borrow.
    pub(crate) fn retire(&mut self) -> (
        VecDeque<BlockSlot>,
        HashMap<u64, Py<Block>>,
        Option<Py<Chain>>,
    ) {
        self.blocks_start_index += self.blocks.len();
        self.forked_block = None;
        (
            std::mem::take(&mut self.blocks),
            std::mem::take(&mut self.forked_blocks),
            self.chain.take(),
        )
    }

    /// Drops blocks above `latest_block_number` (used when reverting).
    ///
    /// The arithmetic is clamped on both ends: reverting to a snapshot below the
    /// retained window used to wrap in release builds, making the truncate a
    /// no-op so stale blocks survived and the next `add_block` tripped its
    /// length check.
    pub fn remove_blocks(&mut self, latest_block_number: u64) -> Vec<BlockSlot> {
        let new_len = (latest_block_number as usize + 1)
            .saturating_sub(self.blocks_start_index)
            .min(self.blocks.len());
        self.blocks.split_off(new_len).into_iter().collect()
    }

    /// Re-seeds the window from a snapshot after reverting to it. The snapshot
    /// does not pin the journal; replay paths validate each restored journal
    /// point at use through `CacheDB::rollback`.
    pub(crate) fn reseed(&mut self, start_index: usize, slots: Vec<BlockSlot>) {
        debug_assert!(self.blocks.is_empty(), "reseed expects a drained window");
        self.blocks_start_index = start_index;
        self.blocks = slots.into();
    }

    pub(crate) fn iter(&self) -> impl Iterator<Item = &BlockSlot> {
        self.blocks.iter()
    }

    fn lookup_mined(&self, py: Python, number: u64) -> PyResult<Py<Block>> {
        let offset = (number as usize).checked_sub(self.blocks_start_index);
        match offset.and_then(|offset| self.blocks.get(offset)) {
            Some(slot) => Ok(slot.block.clone_ref(py)),
            None => Err(HistoryPrunedError::new_err(format!(
                "block {number} is no longer retained: blocks {}..{} are available. Raise \
                 `testing.block_history` in the configuration (or set it to null to disable \
                 pruning) to keep more.",
                self.blocks_start_index,
                self.blocks_start_index + self.blocks.len()
            ))),
        }
    }

    pub fn get_block(
        &mut self,
        py: Python,
        block: BlockEnum,
        last_block_number: u64,
        provider: Option<ProviderWrapper>,
        forked_chain_id: Option<u64>,
    ) -> Result<Py<Block>, PyErr> {
        let chain = self.chain.as_ref().ok_or_else(|| {
            HistoryPrunedError::new_err("block history was discarded on disconnect")
        })?;
        let number = match block {
            BlockEnum::Int(block_number) => {
                if block_number < 0 {
                    last_block_number + 1 + block_number as u64
                } else if block_number as u64 > last_block_number {
                    return Err(PyValueError::new_err("Block number out of range"));
                } else {
                    block_number as u64
                }
            }
            BlockEnum::Latest | BlockEnum::Safe | BlockEnum::Finalized => last_block_number,
            BlockEnum::Pending => {
                let borrowed_chain = chain.borrow(py);
                let mut block_env = borrowed_chain.get_evm()?.block.clone();
                // add pending gas used to the block gas limit
                block_env.gas_limit += borrowed_chain.pending_gas_used;

                return Py::new(
                    py,
                    Block {
                        chain: chain.clone_ref(py),
                        block_hash: B256::ZERO,
                        block_env,
                        journal_index: Some(borrowed_chain.get_evm()?.db().journal_point()),
                        gas_used: borrowed_chain.pending_gas_used,
                    },
                );
            }
            BlockEnum::Earliest => 0,
        };

        match self.forked_block {
            None => self.lookup_mined(py, number),
            Some(forked_block) => {
                if number > forked_block {
                    self.lookup_mined(py, number)
                } else {
                    if let Some(block) = self.forked_blocks.get(&number) {
                        return Ok(block.clone_ref(py));
                    }

                    match provider {
                        Some(provider) => {
                            let block = provider.get_block_by_number(
                                py,
                                number,
                                true,
                                TOKIO_RUNTIME.handle(),
                            );
                            match block {
                                Ok(block) => {
                                    if let Some(block) = block {
                                        let block = Py::new(
                                            py,
                                            Block {
                                                chain: chain.clone_ref(py),
                                                block_hash: block.header.hash,
                                                block_env: header_to_block_env(&block.header, forked_chain_id.unwrap()),
                                                journal_index: None,
                                                gas_used: block.header.gas_used,
                                            },
                                        )?;
                                        self.forked_blocks.insert(number, block.clone_ref(py));
                                        Ok(block)
                                    } else {
                                        info!("Block not found: {}", number);
                                        Err(PyValueError::new_err("Block not found"))
                                    }
                                }
                                Err(e) => Err(PyValueError::new_err(e.to_string())),
                            }
                        }
                        None => Err(PyValueError::new_err("Block not found")),
                    }
                }
            }
        }
    }
}

#[pymethods]
impl Blocks {
    #[new]
    pub(crate) fn new(
        chain: Py<Chain>,
        forked_block: Option<u64>,
    ) -> Self {
        Self {
            chain: Some(chain),
            blocks: VecDeque::new(),
            blocks_start_index: forked_block.map_or(0, |b| b + 1) as usize,
            forked_blocks: HashMap::new(),
            forked_block,
        }
    }

    /// Number of the oldest locally mined block still retained. Older locally
    /// mined blocks raise `HistoryPrunedError`; provider-backed pre-fork blocks
    /// remain available.
    #[getter]
    fn first_number(&self) -> usize {
        self.blocks_start_index
    }

    fn __getitem__(&mut self, py: Python, block: BlockEnum) -> PyResult<Py<Block>> {
        let chain = self.chain.as_ref().ok_or_else(|| {
            HistoryPrunedError::new_err("block history was discarded on disconnect")
        })?.borrow(py);
        let last_block_number = chain.last_block_number()?;
        let provider = chain.provider.clone();
        let forked_chain_id = chain.forked_chain_id;
        drop(chain);
        self.get_block(py, block, last_block_number, provider, forked_chain_id)
    }
}

#[pyclass]
pub struct Block {
    #[pyo3(get)]
    pub chain: Py<Chain>,
    pub block_env: BlockEnv,
    pub block_hash: B256,
    /// End-of-block state for a mined block, or the recorded origin of a pending
    /// view. Provider-backed pre-fork blocks have no local journal point.
    pub journal_index: Option<JournalPoint>,
    #[pyo3(get)]
    pub gas_used: u64,
}

#[pymethods]
impl Block {
    /// Transactions in execution order, resolved from the retained local history.
    /// The returned list owns its references; the block does not cache them.
    #[getter]
    fn txs(slf: &Bound<'_, Self>, py: Python<'_>) -> PyResult<Vec<Py<TransactionAbc>>> {
        let block = slf.borrow();
        let chain = block.chain.borrow(py);
        let point = block.journal_index.ok_or_else(|| PyNotImplementedError::new_err(
            "transactions of provider-backed pre-fork blocks are not available; Block.txs supports locally executed blocks",
        ))?;
        chain.ensure_history(point)?;
        let evm = chain.get_evm()?;
        let number = block.get_number();
        let pending = block.block_hash == B256::ZERO;

        if pending {
            evm.db().check_lineage(point)
                .map_err(|error| HistoryPrunedError::new_err(error.to_string()))?;
            if block.block_env.number == evm.block.number {
                return Ok(chain.pending_txs.iter().map(|tx| tx.clone_ref(py)).collect());
            }
        }

        if number > chain.last_block_number()? {
            return Err(HistoryPrunedError::new_err(format!(
                "block {number} was discarded by a snapshot revert",
            )));
        }
        let blocks = chain
            .blocks
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Not connected"))?
            .borrow(py);
        let canonical = blocks.lookup_mined(py, number)?;
        if !pending && !canonical.bind(py).is(slf) {
            return Err(HistoryPrunedError::new_err(format!(
                "block {number} was discarded by a snapshot revert; its number now denotes a different block",
            )));
        }

        let txs = chain
            .txs
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Not connected"))?
            .borrow(py);
        Ok(txs
            .iter()
            .filter_map(|slot| {
                let tx = slot.tx.borrow(py);
                match &tx.block {
                    BlockInfo::Mined(tx_block) if tx_block.bind(py).is(canonical.bind(py)) => {
                        Some(slot.tx.clone_ref(py))
                    }
                    _ => None,
                }
            })
            .collect())
    }

    #[getter]
    fn get_number(&self) -> u64 {
        self.block_env.number.try_into().unwrap()
    }

    #[getter]
    fn get_timestamp(&self) -> u64 {
        self.block_env.timestamp.try_into().unwrap()
    }

    #[getter]
    fn get_hash(&self) -> String {
        self.block_hash.to_string()
    }
}
