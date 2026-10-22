// SPDX-License-Identifier: MIT
pragma solidity ^0.8.37;

import {Script} from "forge-std/Script.sol";
import {console} from "forge-std/console.sol";
import {DepositForwarderFactory} from "../src/DepositForwarderFactory.sol";

contract DeployDepositForwarderFactory is Script {
    function run() external returns (DepositForwarderFactory depositForwarderFactory) {
        // With DEPLOYER_PRIVATE_KEY in the environment (deploy/deploy_sandbox_factories.sh) it signs with that key, so a
        // real key never sits on a command line; without it, with the forge command's own signer
        // (the e2e's anvil dev key).
        uint256 deployerPrivateKey = vm.envOr("DEPLOYER_PRIVATE_KEY", uint256(0));
        if (deployerPrivateKey != 0) {
            vm.startBroadcast(deployerPrivateKey);
        } else {
            vm.startBroadcast();
        }
        depositForwarderFactory = new DepositForwarderFactory();
        vm.stopBroadcast();
        console.log("DepositForwarderFactory:", address(depositForwarderFactory));
    }
}
