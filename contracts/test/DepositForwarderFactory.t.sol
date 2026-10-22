// SPDX-License-Identifier: MIT
pragma solidity ^0.8.37;

import {Test} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {DepositForwarder} from "../src/DepositForwarder.sol";
import {DepositForwarderFactory} from "../src/DepositForwarderFactory.sol";

contract MockPaymentToken is ERC20 {
    constructor() ERC20("Mock USDC", "USDC") {}

    function mint(address recipient, uint256 amount) external {
        _mint(recipient, amount);
    }
}

contract NativeRejectingDestination {
    receive() external payable {
        revert("Native coin rejected");
    }
}

contract DepositForwarderFactoryTest is Test {
    function testNativeAndTokenDepositsSweepAndRepeatWithOnlyNewMoney() external {
        DepositForwarderFactory depositForwarderFactory = new DepositForwarderFactory();
        MockPaymentToken paymentToken = new MockPaymentToken();
        address payable depositDestination = payable(makeAddr("depositDestination"));
        bytes32 invoiceSalt = keccak256("main invoice");
        address depositAddress = depositForwarderFactory.predictDepositAddress(invoiceSalt, depositDestination);
        address[] memory tokens = new address[](1);
        tokens[0] = address(paymentToken);
        vm.deal(address(this), 3 ether);
        (bool depositSucceeded,) = payable(depositAddress).call{value: 2 ether}("");
        assertTrue(depositSucceeded);
        paymentToken.mint(address(this), 75_000_000);
        paymentToken.transfer(depositAddress, 50_000_000);
        depositForwarderFactory.sweepToDestination(invoiceSalt, depositDestination, tokens);
        assertEq(depositDestination.balance, 2 ether);
        assertEq(paymentToken.balanceOf(depositDestination), 50_000_000);
        assertGt(depositAddress.code.length, 0);
        assertEq(depositForwarderFactory.predictDepositAddress(invoiceSalt, depositDestination), depositAddress);
        assertEq(DepositForwarder(payable(depositAddress)).destination(), depositDestination);
        assertEq(depositAddress.balance, 0);
        assertEq(paymentToken.balanceOf(depositAddress), 0);
        (depositSucceeded,) = payable(depositAddress).call{value: 1 ether}("");
        assertTrue(depositSucceeded);
        paymentToken.transfer(depositAddress, 25_000_000);
        depositForwarderFactory.sweepToDestination(invoiceSalt, depositDestination, tokens);
        assertEq(depositDestination.balance, 3 ether);
        assertEq(paymentToken.balanceOf(depositDestination), 75_000_000);
        assertEq(depositAddress.balance, 0);
        assertEq(paymentToken.balanceOf(depositAddress), 0);
        vm.recordLogs();
        depositForwarderFactory.sweepToDestination(invoiceSalt, depositDestination, tokens);
        assertEq(vm.getRecordedLogs().length, 0);
    }

    function testFuzzAnyCallerCanOnlyForwardToDestinationAndNativeRejectionIsAtomic(address sweepCaller) external {
        DepositForwarderFactory depositForwarderFactory = new DepositForwarderFactory();
        MockPaymentToken paymentToken = new MockPaymentToken();
        address payable depositDestination = payable(makeAddr("fixedDestination"));
        bytes32 invoiceSalt = keccak256("permissionless invoice");
        address depositAddress = depositForwarderFactory.predictDepositAddress(invoiceSalt, depositDestination);
        vm.assume(sweepCaller != depositDestination && sweepCaller != depositAddress);
        address[] memory tokens = new address[](1);
        tokens[0] = address(paymentToken);
        vm.deal(depositAddress, 1 ether);
        paymentToken.mint(depositAddress, 50_000_000);
        uint256 callerNativeBalance = sweepCaller.balance;
        vm.prank(sweepCaller);
        depositForwarderFactory.sweepToDestination(invoiceSalt, depositDestination, tokens);
        assertEq(depositDestination.balance, 1 ether);
        assertEq(paymentToken.balanceOf(depositDestination), 50_000_000);
        assertEq(sweepCaller.balance, callerNativeBalance);
        assertEq(paymentToken.balanceOf(sweepCaller), 0);
        vm.expectRevert(DepositForwarder.OnlyFactory.selector);
        vm.prank(sweepCaller == address(depositForwarderFactory) ? address(this) : sweepCaller);
        DepositForwarder(payable(depositAddress)).forwardAll(tokens);

        address payable rejectingDestination = payable(address(new NativeRejectingDestination()));
        address rejectedDepositAddress =
            depositForwarderFactory.predictDepositAddress(invoiceSalt, rejectingDestination);
        assertNotEq(depositAddress, rejectedDepositAddress);
        vm.deal(rejectedDepositAddress, 2 ether);
        paymentToken.mint(rejectedDepositAddress, 25_000_000);
        vm.recordLogs();
        vm.expectRevert(DepositForwarder.NativeForwardingFailed.selector);
        vm.prank(sweepCaller);
        depositForwarderFactory.sweepToDestination(invoiceSalt, rejectingDestination, tokens);
        assertEq(vm.getRecordedLogs().length, 0);
        assertEq(rejectedDepositAddress.balance, 2 ether);
        assertEq(paymentToken.balanceOf(rejectedDepositAddress), 25_000_000);
        assertEq(rejectedDepositAddress.code.length, 0);
        assertEq(rejectingDestination.balance, 0);
        assertEq(paymentToken.balanceOf(rejectingDestination), 0);
    }
}
