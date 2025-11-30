"""
Word lists for memorable agent name generation.

~500 words total, split into two pools (adjective-ish, noun-ish).
Criteria:
- Memorable, distinct, easy to say
- No profanity, slurs, or awkward combos
- Concrete over abstract (anvil > concept)
- Mix of: tools, animals, materials, weather, objects
"""

# ~250 adjective-ish words
# Colors, textures, qualities, states
ADJECTIVES = [
    # Colors and visual
    "amber", "azure", "bronze", "chrome", "cobalt", "copper", "coral",
    "crimson", "golden", "indigo", "ivory", "jade", "jet", "marble",
    "obsidian", "ochre", "onyx", "opal", "pearl", "ruby", "russet",
    "sable", "sapphire", "scarlet", "silver", "slate", "tawny", "teal",
    "turquoise", "velvet", "vermillion", "violet",

    # Textures and materials
    "braided", "brushed", "burnished", "carved", "cast", "chiseled",
    "coiled", "coarse", "crisp", "crystal", "dappled", "dense", "dusty",
    "etched", "felted", "flint", "forged", "frosted", "glazed", "gnarled",
    "grained", "granite", "hammered", "hewn", "hollow", "iron", "jagged",
    "knotted", "lacquered", "leather", "linen", "matte", "molten", "mossy",
    "notched", "oaken", "pebbled", "polished", "quilted", "ribbed", "rough",
    "rusted", "satin", "scaled", "silken", "smooth", "speckled", "spun",
    "steel", "stone", "striated", "tarnished", "tempered", "threaded",
    "waxed", "weathered", "wicker", "wired", "woven", "wrought",

    # Weather and nature
    "arctic", "autumn", "balmy", "bitter", "blazing", "breezy", "brisk",
    "calm", "clouded", "coastal", "crisp", "dawn", "deep", "desert",
    "dusk", "dusty", "electric", "ember", "fading", "fern", "fierce",
    "floral", "foggy", "forest", "frosty", "glacial", "glowing", "hazy",
    "highland", "lunar", "marsh", "meadow", "midnight", "misty", "monsoon",
    "morning", "mossy", "mountain", "night", "northern", "ocean", "polar",
    "prairie", "radiant", "rain", "rippling", "river", "rocky", "rolling",
    "salt", "shadow", "shimmering", "solar", "spring", "starlit", "storm",
    "summer", "sunset", "tidal", "timber", "twilight", "valley", "vernal",
    "wandering", "wild", "winter", "woodland",

    # Qualities and states
    "able", "agile", "alert", "ancient", "bold", "brave", "bright",
    "brisk", "calm", "clever", "constant", "curious", "daring", "deft",
    "diligent", "eager", "earnest", "faithful", "fearless", "firm", "fleet",
    "frank", "free", "gallant", "gentle", "glad", "grand", "grateful",
    "hardy", "hearty", "honest", "humble", "keen", "kind", "lasting",
    "lively", "loyal", "lucid", "merry", "mighty", "modest", "noble",
    "patient", "peaceful", "plain", "plucky", "proud", "prudent", "pure",
    "quick", "quiet", "ready", "robust", "roving", "sage", "serene",
    "sharp", "shrewd", "silent", "simple", "sincere", "skilled", "sleek",
    "slight", "smart", "smooth", "snug", "sober", "solid", "sound",
    "spare", "spirited", "stable", "stalwart", "stark", "staunch", "steady",
    "stout", "strong", "sturdy", "subtle", "supple", "swift", "tender",
    "thorough", "tidy", "tough", "tranquil", "true", "trusty", "valiant",
    "vigilant", "vivid", "warm", "wary", "watchful", "wise", "witty",
]

# ~250 noun-ish words
# Tools, animals, materials, weather phenomena, objects
NOUNS = [
    # Tools and implements
    "anvil", "awl", "axle", "beacon", "bellows", "blade", "bolt",
    "brace", "bucket", "cable", "chain", "chisel", "clamp", "clasp",
    "cog", "compass", "crane", "dial", "drill", "drum", "file",
    "flint", "forge", "funnel", "gauge", "gear", "gimbal", "hammer",
    "hinge", "hook", "inkwell", "iron", "jack", "key", "kiln",
    "lamp", "lance", "latch", "lathe", "lens", "lever", "lock",
    "loom", "mallet", "mantle", "mill", "mortar", "nail", "needle",
    "nut", "oar", "paddle", "peg", "pestle", "pick", "pillar",
    "pin", "pipe", "pivot", "plank", "plate", "pliers", "plumb",
    "pole", "press", "prism", "pulley", "pump", "rail", "rake",
    "ratchet", "reel", "rivet", "rod", "roller", "rudder", "saddle",
    "sail", "saw", "scale", "screw", "seal", "shaft", "shears",
    "shuttle", "sickle", "sieve", "slab", "sledge", "socket", "spoke",
    "spool", "spring", "stake", "stamp", "staple", "stirrup", "strap",
    "tack", "tang", "tap", "tender", "tiller", "tine", "toggle",
    "tongs", "torch", "tower", "trowel", "valve", "vane", "vault",
    "vice", "wedge", "wheel", "winch", "wire", "wrench", "yoke",

    # Animals
    "badger", "bear", "beaver", "bison", "boar", "bobcat", "buck",
    "bull", "bunting", "buzzard", "cardinal", "condor", "cougar", "crane",
    "crow", "deer", "dove", "drake", "eagle", "elk", "falcon",
    "finch", "fisher", "fox", "goat", "goose", "grouse", "gull",
    "hare", "hawk", "heron", "hound", "ibis", "jay", "kestrel",
    "kinglet", "lark", "lion", "loon", "lynx", "marten", "mink",
    "moose", "moth", "otter", "owl", "ox", "panther", "pelican",
    "perch", "pike", "plover", "puma", "quail", "ram", "raven",
    "robin", "salmon", "seal", "shrike", "sparrow", "stag", "starling",
    "stork", "swan", "swift", "tern", "thrush", "tiger", "trout",
    "viper", "vole", "walrus", "weasel", "wolf", "wren",

    # Natural objects and phenomena
    "ash", "bark", "basin", "bay", "beach", "bluff", "boulder",
    "branch", "brook", "cairn", "canyon", "cape", "cavern", "cedar",
    "clay", "cliff", "cloud", "cove", "creek", "crest", "delta",
    "dune", "eddy", "elm", "ember", "fern", "fjord", "flame",
    "flare", "flax", "floe", "foam", "fog", "frost", "gale",
    "geyser", "glacier", "glade", "glen", "gorge", "granite", "grove",
    "gulf", "gust", "harbor", "heath", "hill", "hollow", "inlet",
    "isle", "kelp", "knoll", "lagoon", "ledge", "lichen", "marsh",
    "meadow", "mesa", "mist", "moss", "oak", "peat", "peak",
    "pine", "pond", "prairie", "quartz", "rapids", "ravine", "reef",
    "ridge", "rill", "shoal", "shore", "shrub", "sleet", "slope",
    "snow", "spring", "spruce", "storm", "strait", "stream", "summit",
    "surf", "swamp", "thaw", "thicket", "thorn", "tide", "timber",
    "vale", "wave", "willow",
]


def get_word_pair(seed: int = None) -> tuple[str, str]:
    """
    Get a random adjective-noun pair.

    Args:
        seed: Optional seed for deterministic selection

    Returns:
        Tuple of (adjective, noun)
    """
    import random

    if seed is not None:
        rng = random.Random(seed)
    else:
        rng = random.Random()

    adj = rng.choice(ADJECTIVES)
    noun = rng.choice(NOUNS)

    return adj, noun
