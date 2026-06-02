import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

const kBg          = Color(0xFF0a0a14);
const kSurface     = Color(0xFF12121f);
const kSurfaceHigh = Color(0xFF1e1e35);
const kCard        = Color(0xFF1a1a2e);
const kBorder      = Color(0xFF2a2a4a);
const kPrimary     = Color(0xFF6aabff);
const kGreen       = Color(0xFF34d399);
const kRed         = Color(0xFFfb7185);
const kAmber       = Color(0xFFfbbf24);
const kCyan        = Color(0xFF22d3ee);
const kPurple      = Color(0xFFa78bfa);
const kMuted       = Color(0xFF6b7280);
const kDim         = kMuted;
const kText        = Color(0xFFe2e8f0);
const kTextPrimary = kText;
const kTextSecondary = Color(0xFF94a3b8);

ThemeData buildTheme() => ThemeData(
  brightness: Brightness.dark,
  scaffoldBackgroundColor: kBg,
  colorScheme: const ColorScheme.dark(
    surface: kSurface,
    primary: kPrimary,
    error: kRed,
  ),
  textTheme: GoogleFonts.interTextTheme(ThemeData.dark().textTheme).apply(
    bodyColor: kText,
    displayColor: kText,
  ),
  appBarTheme: const AppBarTheme(
    backgroundColor: kSurface,
    foregroundColor: kText,
    elevation: 0,
    surfaceTintColor: Colors.transparent,
  ),
  navigationBarTheme: NavigationBarThemeData(
    backgroundColor: kSurface,
    indicatorColor: kPrimary.withAlpha(40),
    labelTextStyle: WidgetStateProperty.all(
      GoogleFonts.inter(fontSize: 11, color: kText),
    ),
  ),
  cardTheme: const CardThemeData(
    color: kCard,
    elevation: 0,
    margin: EdgeInsets.zero,
  ),
  inputDecorationTheme: InputDecorationTheme(
    filled: true,
    fillColor: kCard,
    border: OutlineInputBorder(
      borderRadius: BorderRadius.circular(10),
      borderSide: const BorderSide(color: kBorder),
    ),
    enabledBorder: OutlineInputBorder(
      borderRadius: BorderRadius.circular(10),
      borderSide: const BorderSide(color: kBorder),
    ),
    focusedBorder: OutlineInputBorder(
      borderRadius: BorderRadius.circular(10),
      borderSide: const BorderSide(color: kPrimary),
    ),
    hintStyle: const TextStyle(color: kMuted),
    contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
  ),
  dividerColor: kBorder,
  useMaterial3: true,
);
