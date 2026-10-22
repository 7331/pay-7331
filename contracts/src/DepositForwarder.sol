// SPDX-License-Identifier: MIT
pragma solidity ^0.8.37;

import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

/// @notice PLAN.md section 3: deposits can only leave for their immutable destination.
contract DepositForwarder {
    using SafeERC20 for IERC20;

    address public immutable factory;

    error OnlyFactory();
    error NativeForwardingFailed();

    constructor() {
        factory = msg.sender;
    }

    receive() external payable {}

    function destination() public view returns (address payable) {
        return payable(abi.decode(Clones.fetchCloneArgs(address(this)), (address)));
    }

    function forwardAll(address[] calldata tokens)
        external
        returns (uint256 nativeAmount, uint256[] memory tokenAmounts)
    {
        if (msg.sender != factory) revert OnlyFactory();
        address payable depositDestination = destination();
        nativeAmount = address(this).balance;
        if (nativeAmount != 0) {
            (bool forwardingSucceeded,) = depositDestination.call{value: nativeAmount}("");
            if (!forwardingSucceeded) revert NativeForwardingFailed();
        }
        tokenAmounts = new uint256[](tokens.length);
        for (uint256 tokenIndex; tokenIndex < tokens.length; ++tokenIndex) {
            IERC20 depositToken = IERC20(tokens[tokenIndex]);
            uint256 tokenAmount = depositToken.balanceOf(address(this));
            tokenAmounts[tokenIndex] = tokenAmount;
            if (tokenAmount != 0) depositToken.safeTransfer(depositDestination, tokenAmount);
        }
    }
}
