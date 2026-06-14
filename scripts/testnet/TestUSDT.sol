// SPDX-License-Identifier: MIT
// Minimal TRC20 for Nile testnet E2E. Emits the standard Transfer event so
// TronGrid indexes transfers exactly like real USDT — which is all the swap
// watcher reads. Whole supply is minted to the deployer (the payer wallet).
pragma solidity ^0.8.0;

contract TestUSDT {
    string public name = "Test USDT";
    string public symbol = "USDT";
    uint8 public decimals = 6;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;

    event Transfer(address indexed from, address indexed to, uint256 value);

    constructor(uint256 initialSupply) {
        totalSupply = initialSupply;
        balanceOf[msg.sender] = initialSupply;
        emit Transfer(address(0), msg.sender, initialSupply);
    }

    function transfer(address to, uint256 value) public returns (bool) {
        require(balanceOf[msg.sender] >= value, "insufficient");
        balanceOf[msg.sender] -= value;
        balanceOf[to] += value;
        emit Transfer(msg.sender, to, value);
        return true;
    }
}
