use std::{collections::HashMap, sync::Arc};

use pyo3::{prelude::*, types::PyType};
use revm::context::{BlockEnv, CfgEnv};
use revm::primitives::Address as RevmAddress;

use crate::{
    account::Account,
    blocks::{BlockSlot, Blocks},
    chain::Chain,
    tx::TransactionAbc,
    txs::{TxSlot, Txs},
};

/// The transactions and blocks a snapshot keeps alive so a revert has visible
/// history to land on.
///
/// Pruning truncates the live window from the front, and reverting truncates it
/// from the back at the snapshot's index — which together can leave nothing, so
/// that `chain.blocks["latest"]` would raise immediately after a revert. Holding
/// the references here also gives them their lifecycle for free: they die with
/// the snapshot.
///
/// These references do not pin the journal. Replayability after a revert is
/// checked at use and survives only where the restored journal point still
/// agrees with the current lineage; guaranteeing the whole window would pin the
/// journal floor at its oldest replay point, which for an early campaign
/// snapshot can retain nearly the entire campaign.
struct HistoryWindow {
    txs: Vec<TxSlot>,
    txs_start_index: usize,
    blocks: Vec<BlockSlot>,
    blocks_start_index: usize,
}

/// Where a snapshot sits, which is all that has to be recorded when it is taken.
///
/// The window itself is captured lazily, on the first prune that outlives the
/// snapshot — see [`ChainSnapshot::capture_window`].
struct SnapshotPosition {
    /// Newest block number at snapshot time. The revert truncates above this, so
    /// only blocks at or below it are worth keeping.
    block_number: u64,
    /// Journal index at snapshot time, i.e. the bound `remove_txs` will use.
    journal_index: usize,
}

pub struct ChainSnapshot {
    // EVM state
    cfg_env: CfgEnv,

    // Chain state
    labels: Arc<HashMap<RevmAddress, String>>,
    deployed_libraries: Arc<HashMap<[u8; 17], RevmAddress>>,
    fqn_overrides: Arc<HashMap<RevmAddress, Py<PyType>>>,

    // Latest state
    latest_block_env: Option<BlockEnv>,

    // Pending state
    pub(crate) pending_block_env: BlockEnv,
    pending_txs: Vec<Py<TransactionAbc>>,
    pending_gas_used: u64,

    // Where the snapshot sits, recorded eagerly and cheaply.
    position: SnapshotPosition,
    // The window as of the snapshot, captured on the first prune that would
    // destroy part of it. Stays `None` when nothing is ever pruned while this
    // snapshot is live, in which case truncating the live window on revert is
    // enough — which is also the whole point of deferring it.
    history: Option<HistoryWindow>,

    // Configuration state
    default_tx_account: Option<Py<Account>>,
    default_call_account: Option<Py<Account>>,
    default_estimate_account: Option<Py<Account>>,
    default_access_list_account: Option<Py<Account>>,
    block_gas_limit: u64,
    automine: bool,
}

impl ChainSnapshot {
    pub fn from_chain(chain: &Chain, py: Python) -> PyResult<Self> {
        let evm = chain.get_evm()?;
        // O(1): record only where the snapshot sits. Copying the window here
        // would cost O(window in transactions) *per snapshot*, which is ~800 us
        // for a 130 000-transaction window and is re-paid on every iteration of a
        // revert/replay loop that never prunes anything.
        let position = SnapshotPosition {
            block_number: chain.last_block_number()?,
            journal_index: chain.journal_index()?,
        };

        Ok(Self {
            cfg_env: evm.cfg.clone(),
            labels: chain.labels.clone(),
            deployed_libraries: chain.deployed_libraries.clone(),
            fqn_overrides: chain.fqn_overrides.clone(),
            latest_block_env: chain.latest_block_env.clone(),
            pending_block_env: evm.block.clone(),
            pending_txs: chain.pending_txs.iter().map(|tx| tx.clone_ref(py)).collect(),
            pending_gas_used: chain.pending_gas_used,
            position,
            history: None,
            default_tx_account: chain.default_tx_account.as_ref().map(|account| account.clone_ref(py)),
            default_call_account: chain.default_call_account.as_ref().map(|account| account.clone_ref(py)),
            default_estimate_account: chain.default_estimate_account.as_ref().map(|account| account.clone_ref(py)),
            default_access_list_account: chain.default_access_list_account.as_ref().map(|account| account.clone_ref(py)),
            block_gas_limit: chain.block_gas_limit,
            automine: chain.automine,
        })
    }

    /// Freezes the history this snapshot can be reverted onto, if it has not been
    /// frozen already.
    ///
    /// Called from `Chain::maybe_prune` just before it drops anything, which is
    /// the only moment the window can be lost. Deferring it to here rather than to
    /// `snapshot()` means a snapshot that never outlives a prune costs nothing,
    /// and one that does pays the copy exactly once.
    ///
    /// The result is identical to copying at snapshot time: no prune has run since
    /// the snapshot (or the window would already be frozen), so the live window
    /// still starts where it did, and everything above the snapshot's block is a
    /// suffix the revert would truncate anyway.
    pub fn capture_window(&mut self, py: Python, txs: &Txs, blocks: &Blocks) {
        if self.history.is_some() {
            return;
        }

        // Both windows are sorted, so what belongs to the snapshot is a prefix and
        // the start indices carry over unchanged.
        let block_number = self.position.block_number;
        let journal_index = self.position.journal_index;
        self.history = Some(HistoryWindow {
            txs: txs
                .iter()
                .take_while(|slot| slot.journal_index < journal_index)
                .map(|slot| TxSlot {
                    journal_index: slot.journal_index,
                    tx: slot.tx.clone_ref(py),
                })
                .collect(),
            txs_start_index: txs.start_index(),
            blocks: blocks
                .iter()
                .take_while(|slot| slot.number <= block_number)
                .map(|slot| BlockSlot {
                    journal_index: slot.journal_index,
                    number: slot.number,
                    block: slot.block.clone_ref(py),
                })
                .collect(),
            blocks_start_index: blocks.start_index(),
        });
    }

    /// Whether this snapshot froze a history window that a revert must re-seed
    /// from. `false` means nothing was pruned while it was live, so truncating
    /// the live window is enough.
    pub fn has_history(&self) -> bool {
        self.history.is_some()
    }

    /// Re-seeds the chain's history window from this snapshot.
    ///
    /// The caller must have emptied both windows first.
    pub fn restore_history(&self, py: Python, chain: &Chain) -> PyResult<()> {
        let Some(history) = &self.history else {
            return Ok(());
        };

        let mut txs = chain.txs.as_ref().expect("Not connected").borrow_mut(py);
        txs.reseed(
            history.txs_start_index,
            history
                .txs
                .iter()
                .map(|slot| TxSlot {
                    journal_index: slot.journal_index,
                    tx: slot.tx.clone_ref(py),
                })
                .collect(),
        );
        drop(txs);

        let mut blocks = chain.blocks.as_ref().expect("Not connected").borrow_mut(py);
        blocks.reseed(
            history.blocks_start_index,
            history
                .blocks
                .iter()
                .map(|slot| BlockSlot {
                    journal_index: slot.journal_index,
                    number: slot.number,
                    block: slot.block.clone_ref(py),
                })
                .collect(),
        );
        Ok(())
    }

    /// Inclusive block-hash range this snapshot keeps alive.
    ///
    /// Reverting restores the whole captured block window, not just its tip. Empty
    /// blocks can share a journal point, so blocks from that window may still be
    /// replayable after the revert even when their journal entries would otherwise
    /// look metadata-only. The oldest one can read another 256 blocks back.
    pub fn block_hash_range(&self) -> (u64, u64) {
        let newest = self.position.block_number;
        let oldest = self
            .history
            .as_ref()
            .expect("snapshot history must be captured before pruning block hashes")
            .blocks
            .first()
            .expect("captured snapshot history must contain its tip")
            .number;
        (oldest.saturating_sub(256), newest)
    }

    pub fn restore_to_chain(self, chain: &mut Chain) -> PyResult<()> {
        let evm = chain.get_evm_mut()?;
        evm.cfg = self.cfg_env;
        evm.block = self.pending_block_env;

        chain.labels = self.labels;
        chain.deployed_libraries = self.deployed_libraries;
        chain.fqn_overrides = self.fqn_overrides;
        chain.latest_block_env = self.latest_block_env;
        chain.pending_txs = self.pending_txs;
        chain.pending_gas_used = self.pending_gas_used;
        chain.default_tx_account = self.default_tx_account;
        chain.default_call_account = self.default_call_account;
        chain.default_estimate_account = self.default_estimate_account;
        chain.default_access_list_account = self.default_access_list_account;
        chain.block_gas_limit = self.block_gas_limit;
        chain.automine = self.automine;
        Ok(())
    }
}
