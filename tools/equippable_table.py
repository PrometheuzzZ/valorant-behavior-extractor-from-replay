"""Equippable class path -> display name and category.

Display names are not on the wire, and these are a fixed reference set, not
the game's (13.06 calls CompactPistol_C "Bandit"). Do not rename an entry:
to_valplay_bundle.py publishes it as shot.equippable.name and valplay prices
weapons by that literal (it leaves "Compact Pistol" unpriced and "Bandit" at
an unverified 600), so a rename is valplay's decision, made with the price.

Keys: the 'Package.Class_C' path, the package alone and the
'Default__Class_C' archetype, plus exact measured aliases for asset renames
(no case folding).
"""

# fmt: off

EQUIPPABLE_DEFINITIONS = [
    ('/Game/Characters/_Core/Equippable_Unarmed.Equippable_Unarmed_C', 'Unarmed', 'unarmed'),
    ('/Game/Equippables/Melee/Ability_Melee_Base.Ability_Melee_Base_C', 'Melee', 'melee'),
    ('/Game/Equippables/Bomb/BombEquippable.BombEquippable_C', 'Spike', 'bomb'),
    ('/Game/Equippables/Guns/Sidearms/BasePistol/BasePistol.BasePistol_C', 'Classic', 'sidearm'),
    ('/Game/Equippables/Guns/Sidearms/Slim/SawedOffShotgun.SawedOffShotgun_C', 'Shorty', 'sidearm'),
    ('/Game/Equippables/Guns/Sidearms/AutoPistol/AutomaticPistol.AutomaticPistol_C', 'Frenzy', 'sidearm'),
    ('/Game/Equippables/Guns/Sidearms/Luger/LugerPistol.LugerPistol_C', 'Ghost', 'sidearm'),
    ('/Game/Equippables/Guns/Sidearms/Compact/CompactPistol.CompactPistol_C', 'Compact Pistol', 'sidearm'),
    ('/Game/Equippables/Guns/Sidearms/Revolver/RevolverPistol.RevolverPistol_C', 'Sheriff', 'sidearm'),
    ('/Game/Equippables/Guns/SubMachineGuns/Vector/Vector.Vector_C', 'Stinger', 'smg'),
    ('/Game/Equippables/Guns/SubMachineGuns/MP5/SubMachineGun_MP5.SubMachineGun_MP5_C', 'Spectre', 'smg'),
    ('/Game/Equippables/Guns/Shotguns/PumpShotgun/PumpShotgun.PumpShotgun_C', 'Bucky', 'shotgun'),
    ('/Game/Equippables/Guns/Shotguns/AutoShotgun/AutomaticShotgun.AutomaticShotgun_C', 'Judge', 'shotgun'),
    ('/Game/Equippables/Guns/Rifles/Burst/AssaultRifle_Burst.AssaultRifle_Burst_C', 'Bulldog', 'rifle'),
    ('/Game/Equippables/Guns/Rifles/BattleRifle/BattleRifle.BattleRifle_C', 'Warden', 'rifle'),
    ('/Game/Equippables/Guns/SniperRifles/Dmr/DMR.DMR_C', 'Guardian', 'rifle'),
    ('/Game/Equippables/Guns/Rifles/Carbine/AssaultRifle_ACR.AssaultRifle_ACR_C', 'Phantom', 'rifle'),
    ('/Game/Equippables/Guns/Rifles/AK/AssaultRifle_AK.AssaultRifle_AK_C', 'Vandal', 'rifle'),
    ('/Game/Equippables/Guns/SniperRifles/Leversniper/LeverSniperRifle.LeverSniperRifle_C', 'Marshal', 'sniper_rifle'),
    ('/Game/Equippables/Guns/SniperRifles/Boltsniper/BoltSniper.BoltSniper_C', 'Operator', 'sniper_rifle'),
    ('/Game/Equippables/Guns/SniperRifles/Doublesniper/DS_Gun.DS_Gun_C', 'Outlaw', 'sniper_rifle'),
    ('/Game/Equippables/Guns/HvyMachineGuns/LMG/LightMachineGun.LightMachineGun_C', 'Ares', 'machine_gun'),
    ('/Game/Equippables/Guns/HvyMachineGuns/HMG/HeavyMachineGun.HeavyMachineGun_C', 'Odin', 'machine_gun'),
    ('/Game/Characters/Deadeye/S0/Ability_Q/Gun/Gun_Deadeye_Q_Pistol.Gun_Deadeye_Q_Pistol_C', 'Headhunter', 'ability'),
    ('/Game/Characters/Deadeye/S0/Ability_X/Gun_Giantslayer/Gun_Deadeye_X_Giantslayer_Prototype_FIreRatePrototype.Gun_Deadeye_X_Giantslayer_Prototype_FireRatePrototype_C', 'Tour de Force', 'ability'),
]

EQUIPPABLE_PATH_ALIASES = {
    '/Game/Equippables/Guns/SniperRifles/Dmr/DMR.DMR_C': ('/Game/Equippables/Guns/SniperRifles/DMR/DMR.DMR_C',),
}


def _build_lookup():
    """Every key shape -> (name, category, canonical source path)."""
    out = {}
    for class_path, name, category in EQUIPPABLE_DEFINITIONS:
        for path in (class_path, *EQUIPPABLE_PATH_ALIASES.get(class_path, ())):
            package, _, class_name = path.rpartition('.')
            out.update(dict.fromkeys((path, package, 'Default__' + class_name),
                                     (name, category, class_path)))
    return out


EQUIPPABLE_BY_PATH = _build_lookup()

# fmt: on
