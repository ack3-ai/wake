use std::collections::HashMap;

use alloy::rlp::Buf;
use alloy::rpc::types::AccessList;
use num_bigint::BigUint;
use pyo3::{IntoPyObjectExt, PyTypeInfo, intern};
use pyo3::types::{PyBytes, PyNone};
use pyo3::{prelude::*, types::PyDict};
use revm::context::TxEnv;
use revm::context::result::{ExecutionResult, Output};
use revm::primitives::{B256, TxKind};

use crate::account::Account;
use crate::address::Address;
use crate::chain::{BlockInfo, Chain, access_list_into_py};
use crate::blocks::Block;
use crate::inspectors::fqn_inspector::ErrorMetadata;
use crate::pytypes::{decode_and_normalize, new_unknown_error, resolve_error};
use crate::utils::get_py_objects;


#[pyclass]
pub struct Call {
    #[pyo3(get)]
    chain: Py<Chain>,
    block: BlockInfo,
    journal_index: usize, // used for EVM DB journal rollbacks
    tx_env: TxEnv,
    return_type: Option<Py<PyAny>>,
    result: ExecutionResult,
    abi: Option<Py<PyDict>>,
    errors_metadata: HashMap<[u8; 4], ErrorMetadata>,
    access_list: Option<HashMap<Address, Vec<BigUint>>>,
    cached_error: Option<PyErr>,
    cached_return_value: Option<Py<PyAny>>,
    cached_call_trace: Option<Py<PyAny>>,
}

impl Call {
    pub fn new(
        chain: Py<Chain>,
        block: BlockInfo,
        journal_index: usize,
        tx_env: TxEnv,
        return_type: Option<Py<PyAny>>,
        result: ExecutionResult,
        abi: Option<Py<PyDict>>,
        errors_metadata: HashMap<[u8; 4], ErrorMetadata>,
        access_list: Option<AccessList>,
    ) -> Self {
        Self {
            chain,
            block,
            journal_index,
            tx_env,
            return_type,
            result,
            abi,
            errors_metadata,
            access_list: access_list.map(|access_list| access_list_into_py(access_list)),
            cached_error: None,
            cached_return_value: None,
            cached_call_trace: None,
        }
    }
}

#[pymethods]
impl Call {
    #[getter]
    fn block(&self, py: Python) -> PyResult<Py<Block>> {
        match &self.block {
            BlockInfo::Mined(block) => Ok(block.clone_ref(py)),
            BlockInfo::Pending(_) => {
                // TODO!!!
                let borrowed_chain = self.chain.borrow(py);
                let mut block_env = borrowed_chain.get_evm()?.block.clone();
                let gas_used = borrowed_chain.pending_gas_used;

                // add pending gas used to the block gas limit
                block_env.gas_limit += gas_used;

                return Py::new(
                    py,
                    Block {
                        chain: self.chain.clone_ref(py),
                        block_hash: B256::ZERO,
                        block_env,
                        journal_index: None,
                        gas_used,
                    },
                );
            }
        }
    }

    #[getter]
    fn data<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, self.tx_env.data.0.chunk())
    }

    #[getter]
    fn from_(&self, py: Python) -> PyResult<Py<Account>> {
        Py::new(py, Account::from_address_native(py, self.tx_env.caller, self.chain.clone_ref(py))?)
    }

    #[getter]
    fn to(&self, py: Python) -> PyResult<Option<Py<Account>>> {
        match self.tx_env.kind {
            TxKind::Call(to) => Ok(Some(Py::new(py, Account::from_address_native(py, to, self.chain.clone_ref(py))?)?)),
            TxKind::Create => Ok(None),
        }
    }

    #[getter]
    fn status<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let py_objects = get_py_objects(py);

        match &self.result {
            ExecutionResult::Success { .. } => py_objects.wake_exec_status_enum.bind(py).call1((1,)),
            ExecutionResult::Revert { .. } => py_objects.wake_exec_status_enum.bind(py).call1((0,)),
            ExecutionResult::Halt { .. } => py_objects.wake_exec_status_enum.bind(py).call1((0,)),
        }
    }

    #[getter]
    pub fn error(slf: &Bound<Self>, py: Python) -> PyResult<Option<PyErr>> {
        let borrowed = slf.borrow();

        if let Some(error) = &borrowed.cached_error {
            return Ok(Some(error.clone_ref(py)));
        }

        let error = match &borrowed.result {
            ExecutionResult::Success { .. } => None,
            ExecutionResult::Revert {
                gas: _,
                logs: _,
                output,
            } => Some(PyErr::from_value(
                resolve_error(
                    py,
                    &output,
                    &borrowed.chain,
                    None,
                    Some(slf),
                    &borrowed.errors_metadata,
                    get_py_objects(py),
                )?
                .bind(py)
                .clone(),
            )),
            ExecutionResult::Halt { reason, .. } => {
                let error = get_py_objects(py).wake_halt_exception.bind(py).call1((format!("{:?}", reason),))?;
                error.setattr("call", slf)?;
                Some(PyErr::from_value(error,))
            }
        };
        drop(borrowed);
        slf.borrow_mut().cached_error = error.as_ref().map(|e| e.clone_ref(py));
        Ok(error)
    }

    #[getter]
    fn raw_error(slf: &Bound<Self>, py: Python) -> PyResult<Option<PyErr>> {
        match &slf.borrow().result {
            ExecutionResult::Success { .. } => Ok(None),
            ExecutionResult::Revert {
                gas: _,
                logs: _,
                output,
            } => {
                let error = new_unknown_error(py, output, None, Some(slf), get_py_objects(py))?;
                Ok(Some(PyErr::from_value(error.into_bound(py),)))
            }
            ExecutionResult::Halt { reason, .. } => {
                let error = get_py_objects(py).wake_halt_exception.bind(py).call1((format!("{:?}", reason),))?;
                error.setattr("call", slf)?;
                Ok(Some(PyErr::from_value(error,)))
            }
        }
    }

    #[getter]
    pub fn return_value(slf: &Bound<Self>, py: Python) -> PyResult<Py<PyAny>> {
        let borrowed = slf.borrow();

        if let ExecutionResult::Success { output, .. } = &borrowed.result {
            if let Some(return_value) = &borrowed.cached_return_value {
                return Ok(return_value.clone_ref(py));
            } else if borrowed.return_type.is_none() {
                return Call::raw_return_value(slf, py).map(|r| r.unbind())
            }

            match output {
                Output::Call(data) => {
                    let ret_type = borrowed.return_type.as_ref().unwrap().bind(py);

                    let ret = if ret_type.is(&PyNone::type_object(py)) {
                        PyNone::get(py).into_py_any(py)?
                    } else if let Some(abi) = &borrowed.abi {
                        let py_objects = get_py_objects(py);
                        decode_and_normalize(
                            py,
                            data,
                            abi.bind(py),
                            ret_type,
                            &borrowed.chain,
                            intern!(py, "outputs"),
                            py_objects,
                        )?
                    } else {
                        ret_type.call1((PyBytes::new(py, data),))?.unbind()
                    };

                    drop(borrowed);
                    slf.borrow_mut().cached_return_value = Some(ret.clone_ref(py));

                    Ok(ret)
                }
                Output::Create(code, _) => {
                    let ret = PyBytes::new(py, code).into_any().unbind();

                    drop(borrowed);
                    slf.borrow_mut().cached_return_value = Some(ret.clone_ref(py));

                    Ok(ret)
                }
            }
        } else {
            Err(Call::error(slf, py)?.unwrap())
        }
    }

    #[getter]
    fn raw_return_value<'py>(slf: &Bound<Self>, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let borrowed = slf.borrow();
        if let ExecutionResult::Success { output, .. } = &borrowed.result {
            match output {
                Output::Call(data) => Ok(PyBytes::new(py, &data).into_any()),
                Output::Create(_, address) => Bound::new(
                    py,
                    Account::from_address_native(
                        py,
                        address.unwrap(),
                        borrowed.chain.clone_ref(py),
                    )?,
                )
                .map(|a| a.into_any()),
            }
        } else {
            Err(Call::error(slf, py)?.unwrap())
        }
    }

    #[getter]
    fn call_trace(slf: &Bound<Self>, py: Python) -> PyResult<Py<PyAny>> {
        let borrowed = slf.borrow();

        if let Some(call_trace) = &borrowed.cached_call_trace {
            return Ok(call_trace.clone_ref(py));
        }

        let block_env = match &borrowed.block {
            BlockInfo::Mined(block) => block.borrow(py).block_env.clone(),
            BlockInfo::Pending(block_env) => block_env.clone(),
        };
        let trace = borrowed.chain.borrow_mut(py).get_call_trace(
            py,
            borrowed.journal_index,
            &borrowed.tx_env,
            block_env,
        );

        let py_objects = get_py_objects(py);

        let tmp = py_objects.wake_call_trace.bind(py).call_method1(
            intern!(py, "from_native_trace"),
            (
                trace,
                Address::from(borrowed.tx_env.caller),
                borrowed.chain.clone_ref(py),
            ),
        )?.unbind();
        drop(borrowed);
        slf.borrow_mut().cached_call_trace = Some(tmp.clone_ref(py));

        Ok(tmp)
    }

    #[getter]
    fn access_list(slf: &Bound<Self>, py: Python) -> PyResult<HashMap<Address, Vec<BigUint>>> {
        let borrowed = slf.borrow();

        if let Some(access_list) = &borrowed.access_list {
            return Ok(access_list.clone());
        }

        let block_env = match &borrowed.block {
            BlockInfo::Mined(block) => block.borrow(py).block_env.clone(),
            BlockInfo::Pending(block_env) => block_env.clone(),
        };
        let raw_access_list = borrowed.chain.borrow_mut(py).get_access_list(
            py,
            borrowed.journal_index,
            &borrowed.tx_env,
            block_env,
        )?;
        let access_list = access_list_into_py(raw_access_list);

        drop(borrowed);
        slf.borrow_mut().access_list = Some(access_list.clone());

        Ok(access_list)
    }

    #[getter]
    fn estimated_gas(slf: &Bound<Self>) -> u64 {
        // simply use the gas used from the result
        slf.borrow().result.gas_used()
    }
}