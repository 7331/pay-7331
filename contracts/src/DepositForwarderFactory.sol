// SPDX-License-Identifier: MIT
pragma solidity ^0.8.37;

import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {DepositForwarder} from "./DepositForwarder.sol";

/// @notice PLAN.md section 3: permissionless sweeps, with no owner or administrative path.
contract DepositForwarderFactory is ReentrancyGuard {
    address public immutable implementation;

    error ZeroDestination();

    event DepositSwept(
        address indexed depositAddress, address indexed destination, address indexed token, uint256 amount
    );

    constructor() {
        implementation = address(new DepositForwarder());
    }

    function predictDepositAddress(bytes32 invoiceSalt, address destination) public view returns (address) {
        return Clones.predictDeterministicAddressWithImmutableArgs(
            implementation, abi.encode(destination), invoiceSalt, address(this)
        );
    }

    function sweepToDestination(bytes32 invoiceSalt, address payable destination, address[] calldata tokens)
        external
        nonReentrant
    {
        if (destination == address(0)) revert ZeroDestination();
        address depositAddress = predictDepositAddress(invoiceSalt, destination);
        if (depositAddress.code.length == 0) {
            depositAddress =
                Clones.cloneDeterministicWithImmutableArgs(implementation, abi.encode(destination), invoiceSalt);
        }
        (uint256 nativeAmount, uint256[] memory tokenAmounts) =
            DepositForwarder(payable(depositAddress)).forwardAll(tokens);
        if (nativeAmount != 0) emit DepositSwept(depositAddress, destination, address(0), nativeAmount);
        for (uint256 tokenIndex; tokenIndex < tokens.length; ++tokenIndex) {
            if (tokenAmounts[tokenIndex] != 0) {
                emit DepositSwept(depositAddress, destination, tokens[tokenIndex], tokenAmounts[tokenIndex]);
            }
        }
    }
}
